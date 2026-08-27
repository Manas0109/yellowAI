"""Redemption and cancellation (plan §3, §4).

Everything that writes goes through :data:`write_lock`. One writer, ever — so
no two write paths can interleave, and the redemption cap holds under a burst
of simultaneous checkouts.

That guarantee is only true **within one process**. See the README: a
multi-worker deploy would give each worker its own lock and break it.

The lock is not merely an optimisation over the database-level defences. All
requests share a single ``aiosqlite`` connection, and SQLite cannot nest
transactions on one connection — so with the lock removed, overlapping redeems
do not degrade into a race, they fail outright with "cannot start a transaction
within a transaction". This is verified: sabotaging the lock makes
``tests/test_concurrency.py`` fail with exactly that error.

The database-level defences below (``BEGIN IMMEDIATE``, the conditional
``UPDATE``, the UNIQUE constraints) are therefore a second line of defence
against a *different* failure — a future refactor to a connection pool or
multiple workers, where overlapping transactions become possible and a
read-then-write would silently lose updates. They are not redundant, but they
do not make the lock optional today.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime

import aiosqlite

from app.clock import get_clock, isoformat_utc, parse_utc
from app.db import write_transaction
from app.errors import (
    CustomerAlreadyRedeemed,
    CouponExpired,
    IdempotencyKeyReuse,
    NoRedemptionsLeft,
    OrderAlreadyHasRedemption,
    UnknownCode,
)
from app.schemas import CancelResponse, CouponType, RedeemRequest, RedeemResponse

#: The single writer. Held across the whole redeem/cancel transaction (plan §3).
write_lock = asyncio.Lock()


async def redeem(request: RedeemRequest, idempotency_key: str) -> RedeemResponse:
    """Consume one redemption slot, or raise the single most relevant error.

    The failure checks run in a fixed order — permanent conditions before
    transient ones (``ERROR_PRECEDENCE`` in ``errors.py``) — so a request that
    is simultaneously expired *and* out of slots reports the expiry, and the
    client stops retrying instead of hammering a coupon that will never work.

    A failure raises, which rolls the transaction back and leaves **no side
    effect**. That is what allows a retry to re-evaluate against current state
    rather than replaying a stale rejection.
    """
    async with write_lock:
        # Captured exactly once, inside the lock, and threaded through every
        # check below. A second clock read could straddle the expiry instant
        # and let one request resolve two different ways (plan §3).
        now = get_clock().now()

        async with write_transaction() as tx:
            replay = await _replay_if_seen(tx, idempotency_key, request)
            if replay is not None:
                return replay

            coupon = await _load_coupon(tx, request.code)
            if coupon is None:
                raise UnknownCode(request.code)

            if now >= parse_utc(coupon["expires_at"]):
                raise CouponExpired(request.code)

            if coupon["type"] == CouponType.STANDARD:
                # Deliberately status-blind: a CANCELLED redemption still burns
                # the customer's one shot (plan §5, "ever").
                if await _customer_has_redemption(tx, request.code, request.customer_id):
                    raise CustomerAlreadyRedeemed(request.code, request.customer_id)

            if await _order_has_redemption(tx, request.order_id):
                raise OrderAlreadyHasRedemption(request.order_id)

            # The check and the increment are one statement, so there is no
            # read-then-write window to lose an update in. Redundant while the
            # lock serialises writers, and the thing that would still hold the
            # cap if this ever ran with a connection pool or several workers.
            cursor = await tx.execute(
                "UPDATE coupons SET redeemed_count = redeemed_count + 1"
                " WHERE code = ? AND redeemed_count < max_redemptions",
                (request.code,),
            )
            if cursor.rowcount == 0:
                raise NoRedemptionsLeft(request.code)

            try:
                await tx.execute(
                    "INSERT INTO redemptions (order_id, code, customer_id, coupon_type,"
                    " status, redeemed_at) VALUES (?, ?, ?, ?, 'ACTIVE', ?)",
                    (
                        request.order_id,
                        request.code,
                        request.customer_id,
                        coupon["type"],
                        isoformat_utc(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The service-layer checks above should have caught these. If a
                # constraint fires anyway, report it as the 409 it is rather
                # than a 500 — the DB is the last line of defence, not a bug.
                raise _translate_integrity_error(exc, request) from exc

            remaining = coupon["max_redemptions"] - (coupon["redeemed_count"] + 1)
            response = RedeemResponse(
                remaining=remaining,
                discount_percent=coupon["discount_percent"],
            )

            # Recorded in the same transaction as the increment and the
            # redemption row, so a crash can neither burn a slot without a
            # record of it nor record a key whose redemption never happened.
            # Only successes get here: a failure raised above, rolling this
            # back, which is what lets a retry re-evaluate instead of replaying
            # a stale rejection.
            await tx.execute(
                "INSERT INTO idempotency_keys (key, request_hash, response_body,"
                " created_at) VALUES (?, ?, ?, ?)",
                (
                    idempotency_key,
                    _request_hash(request),
                    response.model_dump_json(),
                    isoformat_utc(now),
                ),
            )

            return response


async def cancel(order_id: str) -> CancelResponse:
    """Return the redemption slot for an order — at most once, ever.

    Always succeeds from the caller's point of view; the route returns 200 for
    every outcome. Cancel is inherently idempotent and must never punish a
    retry, so "nothing to do" is reported in the body, not as an error status.

    ``cancelled`` reports what *this call* did, not the order's end state: a
    second cancel returns ``cancelled: false`` because it refunded nothing.
    """
    async with write_lock:
        now = get_clock().now()

        async with write_transaction() as tx:
            redemption = await _load_redemption(tx, order_id)

            if redemption is None:
                # Unknown order and coupon-less order are indistinguishable here
                # — this service only ever hears about coupon-bearing orders —
                # so a 404 would be wrong for a perfectly legitimate order.
                return CancelResponse(cancelled=False, reason="ORDER_NOT_FOUND")

            if redemption["status"] == "CANCELLED":
                return CancelResponse(cancelled=False, reason="ALREADY_CANCELLED")

            # Guarded so the refund below can only ever follow a state change we
            # actually made. Soft-cancel: rows are never deleted, because the
            # partial unique index needs to keep seeing them (plan §5).
            cursor = await tx.execute(
                "UPDATE redemptions SET status = 'CANCELLED', cancelled_at = ?"
                " WHERE order_id = ? AND status = 'ACTIVE'",
                (isoformat_utc(now), order_id),
            )
            if cursor.rowcount == 0:
                return CancelResponse(cancelled=False, reason="ALREADY_CANCELLED")

            # No expiry check: expiry gates redemption, not refunds. Cancelling
            # an order that used a since-expired coupon still returns its slot.
            await tx.execute(
                "UPDATE coupons SET redeemed_count = redeemed_count - 1"
                " WHERE code = ? AND redeemed_count > 0",
                (redemption["code"],),
            )

            coupon = await _load_coupon(tx, redemption["code"])
            remaining = coupon["max_redemptions"] - coupon["redeemed_count"]
            return CancelResponse(
                cancelled=True,
                code=redemption["code"],
                remaining=remaining,
            )


def _request_hash(request: RedeemRequest) -> str:
    """Fingerprint the request a key was first used with.

    Canonical form — sorted keys, no incidental whitespace — so two logically
    identical bodies always hash the same regardless of how the client
    serialised them.
    """
    canonical = json.dumps(
        {
            "code": request.code,
            "customer_id": request.customer_id,
            "order_id": request.order_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _replay_if_seen(
    tx: aiosqlite.Connection, idempotency_key: str, request: RedeemRequest
) -> RedeemResponse | None:
    """Return the stored response if this key already succeeded.

    There is deliberately no in-flight/PENDING state here. A duplicate key
    arriving while the first request is still running simply blocks on
    :data:`write_lock`, and by the time it gets in, the first transaction has
    committed and its key record is visible — so it replays. That removes an
    entire subsystem: no 409-in-progress, no waiter machinery, and no
    crash-recovery policy for half-written pending rows.

    ``remaining`` in the returned body is the value frozen at the first
    success, not the live counter. Replaying means returning the original
    answer; a client that wants current state can GET the coupon.
    """
    async with tx.execute(
        "SELECT request_hash, response_body FROM idempotency_keys WHERE key = ?",
        (idempotency_key,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return None

    if row["request_hash"] != _request_hash(request):
        # Same key, different request. That is a client bug, and guessing which
        # of the two it meant would be worse than saying so.
        raise IdempotencyKeyReuse(idempotency_key)

    stored = json.loads(row["response_body"])
    return RedeemResponse(**{**stored, "replay": True})


def _translate_integrity_error(
    exc: sqlite3.IntegrityError, request: RedeemRequest
) -> Exception:
    detail = str(exc)
    if "ux_standard_once_per_customer" in detail:
        return CustomerAlreadyRedeemed(request.code, request.customer_id)
    if "order_id" in detail:
        return OrderAlreadyHasRedemption(request.order_id)
    return exc


async def _load_coupon(tx: aiosqlite.Connection, code: str) -> aiosqlite.Row | None:
    async with tx.execute("SELECT * FROM coupons WHERE code = ?", (code,)) as cursor:
        return await cursor.fetchone()


async def _load_redemption(
    tx: aiosqlite.Connection, order_id: str
) -> aiosqlite.Row | None:
    async with tx.execute(
        "SELECT * FROM redemptions WHERE order_id = ?", (order_id,)
    ) as cursor:
        return await cursor.fetchone()


async def _customer_has_redemption(
    tx: aiosqlite.Connection, code: str, customer_id: str
) -> bool:
    async with tx.execute(
        "SELECT 1 FROM redemptions WHERE code = ? AND customer_id = ? LIMIT 1",
        (code, customer_id),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _order_has_redemption(tx: aiosqlite.Connection, order_id: str) -> bool:
    async with tx.execute(
        "SELECT 1 FROM redemptions WHERE order_id = ? LIMIT 1", (order_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def create_coupon(
    code: str,
    max_redemptions: int,
    discount_percent: float,
    expires_at: datetime,
    coupon_type: CouponType,
) -> None:
    """Seed a coupon. Raises ``sqlite3.IntegrityError`` if the code exists.

    Codes are immutable and there is deliberately no update path: allowing a
    re-seed to overwrite a row would reset ``redeemed_count`` and blow the
    redemption cap wide open — the worst available bug in this service.
    """
    now = get_clock().now()
    async with write_lock, write_transaction() as tx:
        await tx.execute(
            "INSERT INTO coupons (code, max_redemptions, discount_percent, expires_at,"
            " type, redeemed_count, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (
                code,
                max_redemptions,
                discount_percent,
                isoformat_utc(expires_at),
                coupon_type.value,
                isoformat_utc(now),
            ),
        )
