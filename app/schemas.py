"""Request and response models (plan §4).

All request validation happens here, at the edge, so service code may assume
well-formed input. Request models set ``extra="forbid"`` — a typo'd field is a
client bug and should be told so, not silently dropped.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    field_validator,
    model_serializer,
)

from app.clock import isoformat_utc, to_utc

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class CouponType(StrEnum):
    STANDARD = "STANDARD"
    STACKABLE = "STACKABLE"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _SparseResponse(BaseModel):
    """A response whose optional keys are omitted rather than serialised null.

    Pydantic has no ``exclude_none`` model config — it is a dump-time argument —
    so we drop the Nones in a model serializer instead. That keeps the guarantee
    with the model rather than relying on every call site to remember it.
    """

    @model_serializer(mode="wrap")
    def _drop_nones(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


class CreateCouponRequest(_Request):
    code: NonEmptyStr
    max_redemptions: int = Field(ge=1)
    discount_percent: float = Field(gt=0, le=100)
    expires_at: datetime
    type: CouponType

    @field_validator("expires_at")
    @classmethod
    def _normalise_expiry(cls, value: datetime) -> datetime:
        # Naive input is assumed UTC (see clock.to_utc); everything is stored
        # and compared in UTC so expiry never depends on host timezone.
        return to_utc(value)


class RedeemRequest(_Request):
    code: NonEmptyStr
    customer_id: NonEmptyStr
    order_id: NonEmptyStr


class CouponCreatedResponse(BaseModel):
    code: str
    max_redemptions: int
    redeemed_count: int
    remaining: int
    expires_at: datetime
    type: CouponType

    @field_serializer("expires_at")
    def _serialise_expiry(self, value: datetime) -> str:
        return isoformat_utc(value)


class CouponResponse(BaseModel):
    code: str
    redeemed_count: int
    remaining: int
    max_redemptions: int


class RedeemResponse(_SparseResponse):
    """A fresh success omits ``replay``; a replayed success sets it to true.

    ``remaining`` on a replay is the value frozen at the first attempt, not the
    live value — the point of a replay is to return the original response.
    """

    success: Literal[True] = True
    remaining: int
    discount_percent: float
    replay: bool | None = None


class CancelResponse(_SparseResponse):
    """Always returned with HTTP 200 — cancel must never punish a retry.

    ``reason`` is present only when nothing was cancelled; ``code`` and
    ``remaining`` only when something was.
    """

    cancelled: bool
    reason: str | None = None
    code: str | None = None
    remaining: int | None = None


class ErrorResponse(BaseModel):
    """The single failure envelope (plan §6). Exactly two keys, always."""

    error: str
    message: str
