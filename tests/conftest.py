"""Shared fixtures.

A temp-file database per test — not ``:memory:``, so the tests exercise the
same WAL-mode file backend the service actually runs on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import clock as clock_module
from app import db, service
from app.main import app

#: Every test that needs a "now" hangs off this, so expiry maths in tests is
#: explicit rather than relative to whenever the suite happens to run.
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def conn(tmp_path):
    """A connected, schema-created database scoped to one test."""
    connection = await db.connect(str(tmp_path / "test.db"))
    try:
        yield connection
    finally:
        await db.disconnect()


@pytest.fixture
def frozen_clock():
    """Pin the app-wide clock, and restore the real one afterwards."""
    frozen = clock_module.FrozenClock(NOW)
    clock_module.set_clock(frozen)
    yield frozen
    clock_module.reset_clock()


async def assert_invariants(connection) -> None:
    """The service's core invariants, checked after every test (plan §8 item 8).

    A test that passes its own assertions while leaving the counter disagreeing
    with the redemption rows has still found a bug. This is where that is
    caught, for every coupon, whether or not the test thought to look.
    """
    async with connection.execute(
        "SELECT code, max_redemptions, redeemed_count FROM coupons"
    ) as cursor:
        coupons = await cursor.fetchall()

    for coupon in coupons:
        code, cap, counter = coupon["code"], coupon["max_redemptions"], coupon["redeemed_count"]
        assert 0 <= counter <= cap, f"{code}: redeemed_count {counter} outside [0, {cap}]"

        async with connection.execute(
            "SELECT COUNT(*) FROM redemptions WHERE code = ? AND status = 'ACTIVE'",
            (code,),
        ) as cursor:
            active = (await cursor.fetchone())[0]

        assert counter == active, (
            f"{code}: stored redeemed_count {counter} disagrees with "
            f"{active} active redemption rows"
        )


@pytest.fixture(autouse=True)
def fresh_write_lock():
    """Give each test its own lock, bound to that test's event loop.

    ``asyncio.Lock`` binds to the first loop that acquires it, and pytest-asyncio
    runs every test on a new loop — so a module-level lock created at import
    raises "bound to a different event loop" from the second test onward. It is
    a test-harness problem only: the real process has one loop for its lifetime.

    Rebinding here also stops a lock left held by a failing test from
    deadlocking every test after it.
    """
    service.write_lock = asyncio.Lock()
    yield
    assert not service.write_lock.locked(), "test finished while still holding write_lock"


@pytest_asyncio.fixture
async def client(conn, frozen_clock):
    """An HTTP client bound to the app, over the per-test database.

    The database is opened by the ``conn`` fixture rather than by the app
    lifespan, so each test gets its own file without racing a shared one.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
        # Checked here rather than on `conn` so it covers exactly the tests that
        # drove the service through its API. The low-level tests in test_db.py
        # write rows directly to prove a constraint fires, and would trip an
        # invariant that only the service is responsible for upholding.
        await assert_invariants(conn)


async def seed_coupon(
    client,
    code="SAVE20",
    max_redemptions=100,
    discount_percent=20.0,
    expires_at=None,
    type="STANDARD",
):
    """Create a coupon over HTTP and return the parsed response."""
    response = await client.post(
        "/coupons",
        json={
            "code": code,
            "max_redemptions": max_redemptions,
            "discount_percent": discount_percent,
            "expires_at": (expires_at or NOW + timedelta(days=30)).isoformat(),
            "type": type,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def redeem(client, code="SAVE20", customer_id="cust-1", order_id="order-1", key=None):
    """Redeem over HTTP.

    The default key is unique per call, so a test that reuses an ``order_id``
    exercises the order check rather than tripping over key reuse first. Tests
    about idempotency pass an explicit ``key``.
    """
    return await client.post(
        "/redeem",
        json={"code": code, "customer_id": customer_id, "order_id": order_id},
        headers={"Idempotency-Key": key or f"key-{uuid4()}"},
    )


async def active_count(conn, code="SAVE20") -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM redemptions WHERE code = ? AND status = 'ACTIVE'",
        (code,),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def key_count(conn) -> int:
    async with conn.execute("SELECT COUNT(*) FROM idempotency_keys") as cursor:
        return (await cursor.fetchone())[0]


async def stored_count(conn, code="SAVE20") -> int:
    async with conn.execute(
        "SELECT redeemed_count FROM coupons WHERE code = ?", (code,)
    ) as cursor:
        return (await cursor.fetchone())[0]
