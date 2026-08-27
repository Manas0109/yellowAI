"""Issue #10: refund exactly once, and what cancellation does *not* undo."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import NOW, active_count, redeem, seed_coupon, stored_count


async def test_cancel_returns_the_slot(client, conn):
    await seed_coupon(client, max_redemptions=50)
    await redeem(client)

    response = await client.post("/orders/order-1/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "code": "SAVE20", "remaining": 50}
    assert await stored_count(conn) == 0


async def test_second_cancel_is_a_no_op(client, conn):
    await seed_coupon(client, max_redemptions=50)
    await redeem(client)
    await client.post("/orders/order-1/cancel")

    response = await client.post("/orders/order-1/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": "ALREADY_CANCELLED"}
    assert await stored_count(conn) == 0  # not -1: no double refund


async def test_cancel_unknown_order_is_a_200_no_op(client):
    response = await client.post("/orders/never-existed/cancel")
    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "reason": "ORDER_NOT_FOUND"}


async def test_cancel_works_on_an_expired_coupon(client, frozen_clock, conn):
    """Expiry gates redemption, not refunds."""
    expires_at = NOW + timedelta(days=1)
    await seed_coupon(client, max_redemptions=10, expires_at=expires_at)
    await redeem(client)

    frozen_clock.set(expires_at + timedelta(days=7))
    response = await client.post("/orders/order-1/cancel")

    assert response.json()["cancelled"] is True
    assert await stored_count(conn) == 0


async def test_row_is_soft_cancelled_not_deleted(client, conn):
    await seed_coupon(client)
    await redeem(client)
    await client.post("/orders/order-1/cancel")

    async with conn.execute(
        "SELECT status, cancelled_at FROM redemptions WHERE order_id = 'order-1'"
    ) as cursor:
        row = await cursor.fetchone()

    assert row["status"] == "CANCELLED"
    assert row["cancelled_at"] is not None


async def test_order_id_stays_consumed_after_cancellation(client):
    await seed_coupon(client)
    await redeem(client)
    await client.post("/orders/order-1/cancel")

    response = await redeem(client, customer_id="cust-2", order_id="order-1")
    assert response.status_code == 409
    assert response.json()["error"] == "ORDER_ALREADY_HAS_REDEMPTION"


async def test_cancel_returns_the_slot_but_not_the_customers_eligibility(client):
    """The 'ever' ruling (plan §5) — the sharpest consequence of the design.

    The global slot comes back, so someone else can use it. The customer who
    cancelled cannot, which is what closes the redeem->cancel->redeem loop.
    """
    await seed_coupon(client, max_redemptions=10)
    await redeem(client, order_id="order-1")
    await client.post("/orders/order-1/cancel")

    assert (await client.get("/coupons/SAVE20")).json()["remaining"] == 10

    response = await redeem(client, order_id="order-2")
    assert response.status_code == 409
    assert response.json()["error"] == "CUSTOMER_ALREADY_REDEEMED"

    # ...but the freed slot is genuinely available to a different customer.
    assert (await redeem(client, customer_id="cust-2", order_id="order-3")).status_code == 200


async def test_counter_never_goes_negative(client, conn):
    await seed_coupon(client, max_redemptions=5)
    await redeem(client)
    for _ in range(5):
        await client.post("/orders/order-1/cancel")

    assert await stored_count(conn) == 0


async def test_cancel_frees_a_slot_for_a_previously_full_coupon(client):
    """A retry after NO_REDEMPTIONS_LEFT can succeed once a slot is freed."""
    await seed_coupon(client, code="REFER", max_redemptions=1, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")

    assert (await redeem(client, code="REFER", order_id="order-2")).status_code == 409
    await client.post("/orders/order-1/cancel")
    assert (await redeem(client, code="REFER", order_id="order-2")).status_code == 200


async def test_counter_agrees_with_active_rows_after_cancellations(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=10, type="STACKABLE")
    for n in range(5):
        await redeem(client, code="REFER", order_id=f"order-{n}")
    for n in range(2):
        await client.post(f"/orders/order-{n}/cancel")

    assert await stored_count(conn, "REFER") == 3
    assert await active_count(conn, "REFER") == 3
