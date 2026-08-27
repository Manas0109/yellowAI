"""Redemption and cancellation (plan §3, §4).

Everything that writes goes through :data:`write_lock`. One writer, ever — so
no two write paths can interleave, and the redemption cap holds under a burst
of simultaneous checkouts.

That guarantee is only true **within one process**. See the README: a
multi-worker deploy would give each worker its own lock and break it. The
database-level defences below (``BEGIN IMMEDIATE``, the conditional ``UPDATE``,
the UNIQUE constraints) are kept precisely so the invariant does not rest
solely on deployment topology.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

import aiosqlite

from app.clock import get_clock, isoformat_utc, parse_utc
from app.db import write_transaction
from app.errors import (
    CustomerAlreadyRedeemed,
    CouponExpired,
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
            # Step 2 in plan §4 — the idempotency-key lookup — lands here in
            # issue #9. It must stay inside this transaction so the key record
            # and the counter move together.

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

            # The check and the increment are one statement, so no read-then-write
            # window exists even if the lock above were removed.
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

            # Step 9 in plan §4 — storing the idempotency key and its response —
            # lands here in issue #9, inside this same transaction.

            remaining = coupon["max_redemptions"] - (coupon["redeemed_count"] + 1)
            return RedeemResponse(
                remaining=remaining,
                discount_percent=coupon["discount_percent"],
            )


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
