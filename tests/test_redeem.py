"""Issue #8: the precedence chain, the expiry boundary, and no-side-effect failures."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import NOW, active_count, redeem, seed_coupon, stored_count


async def test_happy_path_increments_by_exactly_one(client, conn):
    await seed_coupon(client, max_redemptions=50)
    response = await redeem(client)

    assert response.status_code == 200
    assert response.json() == {"success": True, "remaining": 49, "discount_percent": 20.0}
    assert await stored_count(conn) == 1
    assert await active_count(conn) == 1


async def test_unknown_code(client):
    response = await redeem(client, code="NOPE")
    assert response.status_code == 404
    assert response.json()["error"] == "UNKNOWN_CODE"


async def test_valid_strictly_before_expiry(client, frozen_clock):
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, expires_at=expires_at)

    frozen_clock.set(expires_at - timedelta(microseconds=1))
    assert (await redeem(client)).status_code == 200


async def test_expired_at_the_exact_instant(client, frozen_clock):
    """`expires_at` is the first invalid instant, not the last valid one."""
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, expires_at=expires_at)

    frozen_clock.set(expires_at)
    response = await redeem(client)
    assert response.status_code == 410
    assert response.json()["error"] == "COUPON_EXPIRED"


async def test_expired_after_the_instant(client, frozen_clock):
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, expires_at=expires_at)

    frozen_clock.set(expires_at + timedelta(microseconds=1))
    assert (await redeem(client)).status_code == 410


async def test_standard_is_once_per_customer(client):
    await seed_coupon(client)
    await redeem(client, order_id="order-1")

    response = await redeem(client, order_id="order-2")
    assert response.status_code == 409
    assert response.json()["error"] == "CUSTOMER_ALREADY_REDEEMED"


async def test_stackable_has_no_per_customer_limit(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=5, type="STACKABLE")
    for n in range(3):
        response = await redeem(client, code="REFER", order_id=f"order-{n}")
        assert response.status_code == 200

    assert await stored_count(conn, "REFER") == 3


async def test_stackable_still_respects_the_global_cap(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=2, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")
    await redeem(client, code="REFER", order_id="order-2")

    response = await redeem(client, code="REFER", order_id="order-3")
    assert response.status_code == 409
    assert response.json()["error"] == "NO_REDEMPTIONS_LEFT"
    assert await stored_count(conn, "REFER") == 2


async def test_order_id_cannot_be_reused(client):
    await seed_coupon(client, code="REFER", type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")

    response = await redeem(client, code="REFER", customer_id="cust-2", order_id="order-1")
    assert response.status_code == 409
    assert response.json()["error"] == "ORDER_ALREADY_HAS_REDEMPTION"


async def test_no_redemptions_left_does_not_exceed_the_cap(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=1, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")

    response = await redeem(client, code="REFER", order_id="order-2")
    assert response.status_code == 409
    assert response.json()["error"] == "NO_REDEMPTIONS_LEFT"
    assert await stored_count(conn, "REFER") == 1


async def test_precedence_reports_expiry_over_everything_else(client, frozen_clock, conn):
    """Expired + customer already redeemed + zero slots -> COUPON_EXPIRED.

    Permanent conditions beat transient ones, so the client stops retrying
    instead of hammering a coupon that will never work again.
    """
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, max_redemptions=1, expires_at=expires_at)
    await redeem(client, order_id="order-1")

    frozen_clock.set(expires_at)
    response = await redeem(client, order_id="order-2")

    assert response.status_code == 410
    assert response.json()["error"] == "COUPON_EXPIRED"


async def test_precedence_reports_customer_over_capacity(client, conn):
    await seed_coupon(client, max_redemptions=1)
    await redeem(client, order_id="order-1")

    response = await redeem(client, order_id="order-2")
    assert response.json()["error"] == "CUSTOMER_ALREADY_REDEEMED"


async def test_failures_leave_no_side_effect(client, conn):
    """A rejected redemption must not move the counter or record anything."""
    await seed_coupon(client, max_redemptions=1)
    await redeem(client, order_id="order-1")
    before = (await stored_count(conn), await active_count(conn))

    await redeem(client, code="NOPE", order_id="order-x")
    await redeem(client, order_id="order-2")
    await redeem(client, customer_id="cust-9", order_id="order-1")

    assert (await stored_count(conn), await active_count(conn)) == before
    async with conn.execute("SELECT COUNT(*) FROM idempotency_keys") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_missing_idempotency_key_is_422(client):
    await seed_coupon(client)
    response = await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"},
    )
    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}


async def test_stored_counter_agrees_with_active_rows(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=10, type="STACKABLE")
    for n in range(4):
        await redeem(client, code="REFER", order_id=f"order-{n}")

    assert await stored_count(conn, "REFER") == await active_count(conn, "REFER")
