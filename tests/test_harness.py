"""Issue #11: the fixtures themselves behave as the other suites assume."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import NOW, seed_coupon


async def test_smoke_create_then_read(client):
    await seed_coupon(client, max_redemptions=7)
    response = await client.get("/coupons/SAVE20")

    assert response.status_code == 200
    assert response.json()["remaining"] == 7


async def test_wal_is_on_inside_tests(conn):
    async with conn.execute("PRAGMA journal_mode;") as cursor:
        assert (await cursor.fetchone())[0].lower() == "wal"


async def test_no_state_leaks_between_tests_part_one(client):
    """Paired with part two: both seed SAVE20, so a leak makes one of them 409."""
    await seed_coupon(client)


async def test_no_state_leaks_between_tests_part_two(client):
    await seed_coupon(client)


async def test_frozen_clock_is_observed_by_the_app(client, frozen_clock):
    """Pinning to the microsecond changes what the service decides."""
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, expires_at=expires_at)

    frozen_clock.set(expires_at - timedelta(microseconds=1))
    ok = await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"},
        headers={"Idempotency-Key": "k1"},
    )
    assert ok.status_code == 200

    frozen_clock.set(expires_at)
    expired = await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-2", "order_id": "order-2"},
        headers={"Idempotency-Key": "k2"},
    )
    assert expired.status_code == 410


async def test_redeem_helper_defaults_to_unique_keys(client):
    """Two default-key redemptions must not collide as a replay."""
    from tests.conftest import redeem

    await seed_coupon(client, code="REFER", max_redemptions=5, type="STACKABLE")
    first = await redeem(client, code="REFER", order_id="order-1")
    second = await redeem(client, code="REFER", order_id="order-2")

    assert first.status_code == second.status_code == 200
    assert "replay" not in second.json()
