"""FastAPI application: lifespan, exception handlers, routes.

Run with a single worker only::

    uvicorn app.main:app --workers 1

Plan §1: the correctness argument assumes one process. A multi-worker deploy
would give each worker its own ``asyncio.Lock`` and its own connection, and the
redemption cap would no longer hold under a burst.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db, service
from app.errors import CodeAlreadyExists, CouponError, UnknownCode
from app.schemas import (
    CancelResponse,
    CouponCreatedResponse,
    CouponResponse,
    CreateCouponRequest,
    RedeemRequest,
    RedeemResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.connect()
    try:
        yield
    finally:
        await db.disconnect()


app = FastAPI(
    title="Coupon Redemption Service",
    version="0.1.0",
    lifespan=lifespan,
)


def _envelope(error: str, message: str, status: int) -> JSONResponse:
    """The single failure shape (plan §6). Exactly two keys, never `detail`."""
    return JSONResponse(status_code=status, content={"error": error, "message": message})


@app.exception_handler(CouponError)
async def _handle_coupon_error(request: Request, exc: CouponError) -> JSONResponse:
    return _envelope(exc.code, exc.message, exc.http_status)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render Pydantic/header validation failures in the same envelope.

    FastAPI's default is ``{"detail": [...]}``, which would make validation the
    one failure mode clients have to special-case. A missing ``Idempotency-Key``
    header arrives here too, and comes out as a 422 like everything else.
    """
    return _envelope("VALIDATION_ERROR", _describe(exc), 422)


def _describe(exc: RequestValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error.get("loc", ()) if p != "body")
        parts.append(f"{location}: {error.get('msg', 'invalid')}" if location else error.get("msg", "invalid"))
    return "; ".join(parts) or "Request validation failed."


@app.post("/coupons", status_code=201, response_model=CouponCreatedResponse)
async def create_coupon(request: CreateCouponRequest) -> CouponCreatedResponse:
    try:
        await service.create_coupon(
            code=request.code,
            max_redemptions=request.max_redemptions,
            discount_percent=request.discount_percent,
            expires_at=request.expires_at,
            coupon_type=request.type,
        )
    except sqlite3.IntegrityError as exc:
        # Let the primary key detect the conflict rather than reading first —
        # a read-then-write here would be the same race the rest of the service
        # is built to avoid.
        raise CodeAlreadyExists(request.code) from exc

    return CouponCreatedResponse(
        code=request.code,
        max_redemptions=request.max_redemptions,
        redeemed_count=0,
        remaining=request.max_redemptions,
        expires_at=request.expires_at,
        type=request.type,
    )


@app.get("/coupons/{code}", response_model=CouponResponse)
async def get_coupon(code: str) -> CouponResponse:
    """Read the counter. Deliberately does **not** take the write lock.

    With a single writer committing whole transactions, a reader observes state
    strictly before or strictly after a write — never a torn intermediate. So
    this number is correct at all times, not eventually correct, without the
    read having to serialise behind in-flight checkouts.

    Returns the stored counter rather than recomputing it from ``redemptions``.
    That the two agree is an invariant the tests assert, not something the read
    path papers over with a join.
    """
    conn = db.get_db()
    async with conn.execute(
        "SELECT code, max_redemptions, redeemed_count FROM coupons WHERE code = ?",
        (code,),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        raise UnknownCode(code)

    return CouponResponse(
        code=row["code"],
        redeemed_count=row["redeemed_count"],
        remaining=row["max_redemptions"] - row["redeemed_count"],
        max_redemptions=row["max_redemptions"],
    )


@app.post("/redeem", response_model=RedeemResponse)
async def redeem(
    request: RedeemRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1),
) -> RedeemResponse:
    """Consume a redemption slot.

    ``Idempotency-Key`` is required — a missing header fails validation and
    comes back as a 422 in the standard envelope. Key *handling* (replay,
    reuse detection) lands in issue #9; the header is threaded through now.
    """
    return await service.redeem(request, idempotency_key)


@app.post("/orders/{order_id}/cancel", response_model=CancelResponse)
async def cancel_order(order_id: str) -> CancelResponse:
    """Reverse the redemption tied to an order, if any.

    Always 200. Every outcome — refunded, already cancelled, no such order —
    is reported in the body, because a client retrying a cancel it is unsure
    landed should never be handed an error for asking twice.
    """
    return await service.cancel(order_id)
