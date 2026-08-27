"""FastAPI application: lifespan, exception handlers, routes.

Run with a single worker only::

    uvicorn app.main:app --workers 1

Plan §1: the correctness argument assumes one process. A multi-worker deploy
would give each worker its own ``asyncio.Lock`` and its own connection, and the
redemption cap would no longer hold under a burst.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db
from app.errors import CouponError


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
