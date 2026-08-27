"""Shared fixtures.

A temp-file database per test — not ``:memory:``, so the tests exercise the
same WAL-mode file backend the service actually runs on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import clock as clock_module
from app import db
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


@pytest_asyncio.fixture
async def client(conn, frozen_clock):
    """An HTTP client bound to the app, over the per-test database.

    The database is opened by the ``conn`` fixture rather than by the app
    lifespan, so each test gets its own file without racing a shared one.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


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
    """Redeem over HTTP with a per-call idempotency key by default."""
    return await client.post(
        "/redeem",
        json={"code": code, "customer_id": customer_id, "order_id": order_id},
        headers={"Idempotency-Key": key or f"key-{order_id}"},
    )


async def active_count(conn, code="SAVE20") -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM redemptions WHERE code = ? AND status = 'ACTIVE'",
        (code,),
    ) as cursor:
        return (await cursor.fetchone())[0]


async def stored_count(conn, code="SAVE20") -> int:
    async with conn.execute(
        "SELECT redeemed_count FROM coupons WHERE code = ?", (code,)
    ) as cursor:
        return (await cursor.fetchone())[0]
