"""Issue #14: the suite that turns the concurrency argument from prose into evidence.

Every burst runs through the real HTTP stack over the real WAL file. Counts are
asserted exactly — never "roughly 50" — because an off-by-one under contention
is precisely the bug these tests exist to catch.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from tests.conftest import active_count, redeem, seed_coupon, stored_count


def outcomes(responses) -> Counter:
    """Tally responses by success or error code."""
    tally = Counter()
    for response in responses:
        body = response.json()
        tally["success" if response.status_code == 200 else body.get("error")] += 1
    return tally


async def test_oversubscription_burst(client, conn):
    """200 simultaneous checkouts, 50 slots. Exactly 50 win.

    This is the flash-sale case from the problem statement. If the check and
    the increment were separate, or the lock were absent, the losers would race
    each other past the cap and the counter would overshoot.
    """
    await seed_coupon(client, max_redemptions=50)

    responses = await asyncio.gather(
        *(
            redeem(client, customer_id=f"cust-{n}", order_id=f"order-{n}")
            for n in range(200)
        )
    )

    tally = outcomes(responses)
    assert tally == {"success": 50, "NO_REDEMPTIONS_LEFT": 150}

    assert (await client.get("/coupons/SAVE20")).json() == {
        "code": "SAVE20",
        "redeemed_count": 50,
        "remaining": 0,
        "max_redemptions": 50,
    }
    assert await stored_count(conn) == await active_count(conn) == 50


async def test_remaining_values_are_unique_across_the_burst(client):
    """No two winners may be told the same `remaining`.

    A stronger check than the final count: it catches a lost update that
    happens to end on the right total.
    """
    await seed_coupon(client, max_redemptions=30)

    responses = await asyncio.gather(
        *(
            redeem(client, customer_id=f"cust-{n}", order_id=f"order-{n}")
            for n in range(60)
        )
    )

    remaining = sorted(r.json()["remaining"] for r in responses if r.status_code == 200)
    assert remaining == list(range(30))


async def test_idempotent_burst(client, conn):
    """50 simultaneous retries of one request produce exactly one redemption.

    This is the in-flight duplicate case. The losers block on the module-level
    lock; by the time each acquires it the first transaction has committed, so
    they find the key record and replay it. No PENDING state, no
    409-in-progress, no waiter machinery — the lock does that work for free.
    """
    await seed_coupon(client, max_redemptions=50)

    responses = await asyncio.gather(
        *(redeem(client, key="one-key") for _ in range(50))
    )

    assert all(r.status_code == 200 for r in responses)

    fresh = [r for r in responses if "replay" not in r.json()]
    replays = [r for r in responses if r.json().get("replay") is True]
    assert len(fresh) == 1
    assert len(replays) == 49

    # Every replay is byte-identical to the others and carries the header.
    assert len({r.text for r in replays}) == 1
    assert all(r.headers.get("Idempotency-Replayed") == "true" for r in replays)
    assert all("Idempotency-Replayed" not in r.headers for r in fresh)

    assert await stored_count(conn) == 1
    async with conn.execute("SELECT COUNT(*) FROM redemptions") as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_double_cancel_burst(client, conn):
    """N simultaneous cancels of one order refund exactly one slot."""
    await seed_coupon(client, max_redemptions=50)
    await redeem(client)
    assert await stored_count(conn) == 1

    responses = await asyncio.gather(
        *(client.post("/orders/order-1/cancel") for _ in range(40))
    )

    assert all(r.status_code == 200 for r in responses)

    refunded = [r for r in responses if r.json()["cancelled"] is True]
    no_ops = [r for r in responses if r.json()["cancelled"] is False]
    assert len(refunded) == 1
    assert len(no_ops) == 39
    assert all(r.json()["reason"] == "ALREADY_CANCELLED" for r in no_ops)

    assert await stored_count(conn) == 0


async def test_mixed_redeem_and_cancel_burst(client, conn):
    """Redeems and cancels interleaved on one coupon keep the books balanced."""
    await seed_coupon(client, code="REFER", max_redemptions=20, type="STACKABLE")

    seeded = await asyncio.gather(
        *(redeem(client, code="REFER", order_id=f"seed-{n}") for n in range(10))
    )
    assert all(r.status_code == 200 for r in seeded)

    await asyncio.gather(
        *(
            *(redeem(client, code="REFER", order_id=f"new-{n}") for n in range(20)),
            *(client.post(f"/orders/seed-{n}/cancel") for n in range(10)),
        )
    )

    counter = await stored_count(conn, "REFER")
    assert counter == await active_count(conn, "REFER")
    assert 0 <= counter <= 20


async def test_burst_against_a_standard_coupon_by_one_customer(client, conn):
    """One customer, many simultaneous checkouts: exactly one may land."""
    await seed_coupon(client, max_redemptions=50)

    responses = await asyncio.gather(
        *(redeem(client, order_id=f"order-{n}") for n in range(50))
    )

    tally = outcomes(responses)
    assert tally == {"success": 1, "CUSTOMER_ALREADY_REDEEMED": 49}
    assert await stored_count(conn) == 1
