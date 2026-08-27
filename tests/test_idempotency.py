"""Issue #9: replay, key reuse, and what is deliberately *not* stored."""

from __future__ import annotations

from tests.conftest import redeem, seed_coupon, stored_count


async def test_replay_returns_the_original_body(client, conn):
    await seed_coupon(client, max_redemptions=50)
    first = await redeem(client, key="key-1")

    second = await redeem(client, key="key-1")

    assert second.status_code == 200
    assert second.json() == {**first.json(), "replay": True}
    assert second.headers["Idempotency-Replayed"] == "true"
    assert await stored_count(conn) == 1


async def test_fresh_success_has_no_replay_marker(client):
    await seed_coupon(client)
    response = await redeem(client, key="key-1")

    assert "replay" not in response.json()
    assert "Idempotency-Replayed" not in response.headers


async def test_replayed_remaining_is_frozen_at_first_success(client):
    """The replay returns the original answer, not the current one."""
    await seed_coupon(client, code="REFER", max_redemptions=10, type="STACKABLE")
    first = await redeem(client, code="REFER", order_id="order-1", key="key-1")
    assert first.json()["remaining"] == 9

    for n in range(2, 5):
        await redeem(client, code="REFER", order_id=f"order-{n}")
    assert (await client.get("/coupons/REFER")).json()["remaining"] == 6

    replay = await redeem(client, code="REFER", order_id="order-1", key="key-1")
    assert replay.json()["remaining"] == 9


async def test_same_key_different_body_is_rejected(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=10, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1", key="key-1")

    response = await redeem(client, code="REFER", order_id="order-2", key="key-1")

    assert response.status_code == 422
    assert response.json()["error"] == "IDEMPOTENCY_KEY_REUSE"
    assert await stored_count(conn, "REFER") == 1


async def test_key_scope_is_global_not_per_customer(client):
    await seed_coupon(client, code="REFER", max_redemptions=10, type="STACKABLE")
    await redeem(client, code="REFER", customer_id="cust-1", order_id="order-1", key="k")

    response = await redeem(
        client, code="REFER", customer_id="cust-2", order_id="order-2", key="k"
    )
    assert response.status_code == 422


async def test_a_failed_redeem_stores_no_key(client, conn):
    await seed_coupon(client, code="REFER", max_redemptions=1, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")

    failed = await redeem(client, code="REFER", order_id="order-2", key="key-retry")
    assert failed.json()["error"] == "NO_REDEMPTIONS_LEFT"

    async with conn.execute(
        "SELECT COUNT(*) FROM idempotency_keys WHERE key = 'key-retry'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_retry_after_a_failure_re_evaluates(client):
    """The payoff of storing only successes.

    The first attempt is refused because the coupon is full. A cancellation
    frees a slot, and the *same* idempotency key then succeeds for real —
    rather than replaying a rejection that is no longer true.
    """
    await seed_coupon(client, code="REFER", max_redemptions=1, type="STACKABLE")
    await redeem(client, code="REFER", order_id="order-1")

    refused = await redeem(client, code="REFER", order_id="order-2", key="key-retry")
    assert refused.status_code == 409

    await client.post("/orders/order-1/cancel")

    retried = await redeem(client, code="REFER", order_id="order-2", key="key-retry")
    assert retried.status_code == 200
    assert "replay" not in retried.json()


async def test_key_and_increment_are_in_one_transaction(client, conn):
    await seed_coupon(client, max_redemptions=5)
    await redeem(client, key="key-1")

    async with conn.execute("SELECT COUNT(*) FROM idempotency_keys") as cursor:
        keys = (await cursor.fetchone())[0]

    assert keys == 1
    assert await stored_count(conn) == 1


async def test_request_hash_is_canonical(client):
    """Key order in the client's JSON must not affect the fingerprint."""
    await seed_coupon(client)
    await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"},
        headers={"Idempotency-Key": "key-1"},
    )

    reordered = await client.post(
        "/redeem",
        json={"order_id": "order-1", "code": "SAVE20", "customer_id": "cust-1"},
        headers={"Idempotency-Key": "key-1"},
    )

    assert reordered.status_code == 200
    assert reordered.json()["replay"] is True
