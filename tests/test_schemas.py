"""Issue #5 acceptance criteria."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    CancelResponse,
    CouponType,
    CreateCouponRequest,
    RedeemRequest,
    RedeemResponse,
)


def coupon(**overrides):
    payload = {
        "code": "SAVE20",
        "max_redemptions": 100,
        "discount_percent": 20.0,
        "expires_at": "2026-12-31T23:59:59Z",
        "type": "STANDARD",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("value", [0, -1])
def test_max_redemptions_must_be_positive(value):
    with pytest.raises(ValidationError):
        CreateCouponRequest(**coupon(max_redemptions=value))


@pytest.mark.parametrize("value", [0, -5, 100.1, 101])
def test_discount_percent_out_of_range_is_rejected(value):
    with pytest.raises(ValidationError):
        CreateCouponRequest(**coupon(discount_percent=value))


def test_discount_percent_of_100_is_accepted():
    assert CreateCouponRequest(**coupon(discount_percent=100)).discount_percent == 100


def test_naive_expiry_is_normalised_to_utc():
    parsed = CreateCouponRequest(**coupon(expires_at="2026-12-31T23:59:59"))
    assert parsed.expires_at == datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def test_offset_expiry_is_converted_to_utc():
    parsed = CreateCouponRequest(**coupon(expires_at="2026-01-01T09:00:00+05:30"))
    assert parsed.expires_at == datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc)


def test_unparseable_expiry_is_rejected():
    with pytest.raises(ValidationError):
        CreateCouponRequest(**coupon(expires_at="not-a-date"))


def test_unknown_type_is_rejected():
    with pytest.raises(ValidationError):
        CreateCouponRequest(**coupon(type="MYSTERY"))


def test_known_types_are_accepted():
    assert CreateCouponRequest(**coupon(type="STACKABLE")).type is CouponType.STACKABLE


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        CreateCouponRequest(**coupon(discount_percentage=20))


@pytest.mark.parametrize("field", ["code", "customer_id", "order_id"])
def test_empty_identifiers_are_rejected(field):
    payload = {"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"}
    payload[field] = ""
    with pytest.raises(ValidationError):
        RedeemRequest(**payload)


def test_fresh_success_omits_the_replay_key():
    dumped = RedeemResponse(remaining=99, discount_percent=20.0).model_dump()
    assert dumped == {"success": True, "remaining": 99, "discount_percent": 20.0}


def test_replay_sets_the_marker():
    dumped = RedeemResponse(remaining=99, discount_percent=20.0, replay=True).model_dump()
    assert dumped["replay"] is True


def test_cancel_success_omits_reason():
    dumped = CancelResponse(cancelled=True, code="SAVE20", remaining=50).model_dump()
    assert dumped == {"cancelled": True, "code": "SAVE20", "remaining": 50}


def test_cancel_noop_omits_code_and_remaining():
    dumped = CancelResponse(cancelled=False, reason="ORDER_NOT_FOUND").model_dump()
    assert dumped == {"cancelled": False, "reason": "ORDER_NOT_FOUND"}
