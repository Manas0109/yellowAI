"""The three acceptance scenarios, written to be read as evidence.

Run with::

    uv run pytest tests/test_acceptance.py -v -s

Each test prints the numbers it asserted on, so the output stands on its own
without the reader having to trust a green dot.

SCOPE NOTE: these run one app process against a file-backed SQLite database.
Correctness here rests on a module-level asyncio.Lock, so the results below
demonstrate the rules hold under concurrency *within a process*. They are not
evidence for a two-instance deployment against a shared database — that
requires moving the guarantee into the database itself, and is out of scope
for this MVP.
"""

from __future__ import annotations

import asyncio
from collections import Counter

from tests.conftest import active_count, redeem, seed_coupon, stored_count

MAX_REDEMPTIONS = 50
BURST = 200


def report(title: str, rows: list[tuple[str, object]]) -> None:
    width = max(len(label) for label, _ in rows)
    print(f"\n{'─' * 68}\n{title}\n{'─' * 68}")
    for label, value in rows:
        print(f"  {label.ljust(width)}  {value}")
    print()


async def test_scenario_1_burst_cannot_exceed_max_redemptions(client, conn, probe):
    """200 simultaneous checkouts against 50 slots. The count must be exact."""
    await seed_coupon(client, max_redemptions=MAX_REDEMPTIONS)
    recorder = probe("Oversubscription burst", BURST)

    responses = await asyncio.gather(
        *(
            redeem(client, customer_id=f"cust-{n}", order_id=f"order-{n}")
            for n in range(BURST)
        )
    )

    tally = Counter(
        "success" if r.status_code == 200 else r.json()["error"] for r in responses
    )
    final = (await client.get("/coupons/SAVE20")).json()

    report(
        "SCENARIO 1 — burst of simultaneous redemptions cannot oversell",
        [
            *recorder.summary(),
            ("Coupon max_redemptions", MAX_REDEMPTIONS),
            ("Simultaneous POST /redeem", BURST),
            ("Succeeded", tally["success"]),
            ("Rejected NO_REDEMPTIONS_LEFT", tally["NO_REDEMPTIONS_LEFT"]),
            ("Any other error code", {k: v for k, v in tally.items()
                                      if k not in ("success", "NO_REDEMPTIONS_LEFT")} or "none"),
            ("Final redeemed_count", final["redeemed_count"]),
            ("Final remaining", final["remaining"]),
            ("ACTIVE redemption rows", await active_count(conn)),
        ],
    )

    assert tally["success"] == MAX_REDEMPTIONS
    assert tally["NO_REDEMPTIONS_LEFT"] == BURST - MAX_REDEMPTIONS
    assert set(tally) == {"success", "NO_REDEMPTIONS_LEFT"}
    assert final["redeemed_count"] == MAX_REDEMPTIONS
    assert final["remaining"] == 0
    # If the calls never overlapped, the burst proved nothing about contention.
    assert recorder.peak > 1, "requests did not actually run concurrently"
    # The stored counter and the actual rows must agree — a counter that is
    # right by luck while the ledger disagrees is still a bug.
    assert await stored_count(conn) == await active_count(conn) == MAX_REDEMPTIONS

    # No winner may have been told the same `remaining` as another; that would
    # mean a lost update that happened to end on the right total.
    remaining_values = sorted(r.json()["remaining"] for r in responses if r.status_code == 200)
    assert remaining_values == list(range(MAX_REDEMPTIONS))


async def test_scenario_2_retry_with_same_key_is_charged_once(client, conn):
    """The client times out and retries. The slot must be consumed once."""
    await seed_coupon(client, max_redemptions=MAX_REDEMPTIONS)

    first = await redeem(client, key="checkout-abc-123")
    second = await redeem(client, key="checkout-abc-123")
    third = await redeem(client, key="checkout-abc-123")

    final = (await client.get("/coupons/SAVE20")).json()

    report(
        "SCENARIO 2 — retrying the same Idempotency-Key charges once",
        [
            ("Idempotency-Key", "checkout-abc-123"),
            ("Attempt 1", f"{first.status_code} {first.json()}"),
            ("Attempt 2", f"{second.status_code} {second.json()}"),
            ("Attempt 3", f"{third.status_code} {third.json()}"),
            ("Idempotency-Replayed (1)", first.headers.get("Idempotency-Replayed", "absent")),
            ("Idempotency-Replayed (2)", second.headers.get("Idempotency-Replayed", "absent")),
            ("redeemed_count", final["redeemed_count"]),
            ("Redemption rows", await active_count(conn)),
        ],
    )

    assert first.status_code == second.status_code == third.status_code == 200
    assert "replay" not in first.json()
    assert second.json()["replay"] is third.json()["replay"] is True

    # The retries return the original answer, byte for byte, not a fresh one.
    assert second.text == third.text
    assert second.json()["remaining"] == first.json()["remaining"]
    assert second.headers["Idempotency-Replayed"] == "true"
    assert "Idempotency-Replayed" not in first.headers

    assert final["redeemed_count"] == 1
    assert await active_count(conn) == 1


async def test_scenario_2b_simultaneous_retries_are_charged_once(client, conn, probe):
    """The harder version: the retry arrives while the first is still in flight."""
    await seed_coupon(client, max_redemptions=MAX_REDEMPTIONS)
    recorder = probe("Idempotent burst", 50, every=10)

    responses = await asyncio.gather(*(redeem(client, key="inflight-key") for _ in range(50)))

    fresh = [r for r in responses if "replay" not in r.json()]
    replays = [r for r in responses if r.json().get("replay") is True]
    final = (await client.get("/coupons/SAVE20")).json()

    report(
        "SCENARIO 2b — 50 *simultaneous* retries of one key charge once",
        [
            *recorder.summary(),
            ("Simultaneous POST /redeem", len(responses)),
            ("Shared Idempotency-Key", "inflight-key"),
            ("Real redemptions", len(fresh)),
            ("Replays", len(replays)),
            ("Distinct replay bodies", len({r.text for r in replays})),
            ("All replays flagged", all(
                r.headers.get("Idempotency-Replayed") == "true" for r in replays)),
            ("redeemed_count", final["redeemed_count"]),
            ("Redemption rows", await active_count(conn)),
        ],
    )

    assert len(fresh) == 1
    assert len(replays) == 49
    assert len({r.text for r in replays}) == 1
    assert final["redeemed_count"] == 1
    assert await active_count(conn) == 1


async def test_scenario_3_double_cancel_returns_the_slot_once(client, conn):
    """Cancelling twice must not refund the slot twice."""
    await seed_coupon(client, max_redemptions=MAX_REDEMPTIONS)
    await redeem(client, order_id="order-1")
    after_redeem = (await client.get("/coupons/SAVE20")).json()["redeemed_count"]

    first = await client.post("/orders/order-1/cancel")
    second = await client.post("/orders/order-1/cancel")
    third = await client.post("/orders/order-1/cancel")

    final = (await client.get("/coupons/SAVE20")).json()

    report(
        "SCENARIO 3 — cancelling twice returns the slot once",
        [
            ("redeemed_count after redeem", after_redeem),
            ("Cancel 1", f"{first.status_code} {first.json()}"),
            ("Cancel 2", f"{second.status_code} {second.json()}"),
            ("Cancel 3", f"{third.status_code} {third.json()}"),
            ("Final redeemed_count", final["redeemed_count"]),
            ("Final remaining", final["remaining"]),
            ("Net slots returned", after_redeem - final["redeemed_count"]),
        ],
    )

    assert after_redeem == 1
    # Every call is a 200 — a client retrying a cancel it is unsure landed must
    # never be handed an error for asking twice.
    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json() == {"cancelled": True, "code": "SAVE20", "remaining": MAX_REDEMPTIONS}
    assert second.json() == {"cancelled": False, "reason": "ALREADY_CANCELLED"}
    assert third.json() == second.json()

    assert final["redeemed_count"] == 0
    assert after_redeem - final["redeemed_count"] == 1
    assert await stored_count(conn) == await active_count(conn) == 0


async def test_scenario_3b_simultaneous_cancels_return_the_slot_once(client, conn, probe):
    """40 concurrent cancels of one order still refund exactly one slot."""
    await seed_coupon(client, max_redemptions=MAX_REDEMPTIONS)
    await redeem(client, order_id="order-1")
    recorder = probe("Double-cancel burst", 40, every=10)

    responses = await asyncio.gather(
        *(client.post("/orders/order-1/cancel") for _ in range(40))
    )

    refunded = [r for r in responses if r.json()["cancelled"] is True]
    final = (await client.get("/coupons/SAVE20")).json()

    report(
        "SCENARIO 3b — 40 *simultaneous* cancels return the slot once",
        [
            *recorder.summary(),
            ("Simultaneous cancels", len(responses)),
            ("All HTTP 200", all(r.status_code == 200 for r in responses)),
            ("cancelled: true", len(refunded)),
            ("cancelled: false (ALREADY_CANCELLED)", len(responses) - len(refunded)),
            ("Final redeemed_count", final["redeemed_count"]),
            ("Net slots returned", 1 - final["redeemed_count"]),
        ],
    )

    assert all(r.status_code == 200 for r in responses)
    assert len(refunded) == 1
    assert final["redeemed_count"] == 0
