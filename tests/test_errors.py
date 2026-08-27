"""Issue #3 acceptance criteria: the code -> status mapping and the envelope."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import errors
from app.main import _handle_coupon_error, _handle_validation_error

EXPECTED_MAPPING = [
    (errors.UnknownCode("SAVE20"), "UNKNOWN_CODE", 404),
    (errors.CouponExpired("SAVE20"), "COUPON_EXPIRED", 410),
    (errors.CustomerAlreadyRedeemed("SAVE20", "cust-1"), "CUSTOMER_ALREADY_REDEEMED", 409),
    (errors.OrderAlreadyHasRedemption("order-1"), "ORDER_ALREADY_HAS_REDEMPTION", 409),
    (errors.NoRedemptionsLeft("SAVE20"), "NO_REDEMPTIONS_LEFT", 409),
    (errors.CodeAlreadyExists("SAVE20"), "CODE_ALREADY_EXISTS", 409),
    (errors.IdempotencyKeyReuse("key-1"), "IDEMPOTENCY_KEY_REUSE", 422),
]


@pytest.mark.parametrize("exc,code,status", EXPECTED_MAPPING)
def test_code_and_status_mapping(exc, code, status):
    assert exc.code == code
    assert exc.http_status == status
    assert exc.message


def test_no_redemptions_left_message_matches_the_plan_example():
    assert errors.NoRedemptionsLeft("SAVE20").message == "Coupon SAVE20 is fully redeemed."


def test_precedence_puts_permanent_conditions_before_transient_ones():
    assert errors.ERROR_PRECEDENCE == (
        "UNKNOWN_CODE",
        "COUPON_EXPIRED",
        "CUSTOMER_ALREADY_REDEEMED",
        "ORDER_ALREADY_HAS_REDEMPTION",
        "NO_REDEMPTIONS_LEFT",
    )


@pytest.fixture
def envelope_client():
    """A throwaway app wired to the real handlers, with no DB lifespan."""
    app = FastAPI()
    app.add_exception_handler(errors.CouponError, _handle_coupon_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)

    class Body(BaseModel):
        n: int

    @app.get("/boom/{code}")
    async def boom(code: str):
        raise next(e for e, c, _ in EXPECTED_MAPPING if c == code)

    @app.post("/validate")
    async def validate(body: Body):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("exc,code,status", EXPECTED_MAPPING)
def test_handler_renders_exactly_two_keys(envelope_client, exc, code, status):
    response = envelope_client.get(f"/boom/{code}")
    assert response.status_code == status
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == code


def test_validation_failure_uses_the_same_envelope(envelope_client):
    response = envelope_client.post("/validate", json={"n": "not-an-int"})
    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}
    assert response.json()["error"] == "VALIDATION_ERROR"
    assert "detail" not in response.json()


def test_every_message_names_the_identifier_at_fault():
    """A log line has to be actionable without the request body next to it."""
    assert "cust-1" in errors.CustomerAlreadyRedeemed("SAVE20", "cust-1").message
    assert "order-1" in errors.OrderAlreadyHasRedemption("order-1").message
    assert "key-1" in errors.IdempotencyKeyReuse("key-1").message
    for exc, _, _ in EXPECTED_MAPPING:
        assert exc.message.endswith(".")


def test_missing_body_uses_the_same_envelope(envelope_client):
    response = envelope_client.post("/validate")
    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}


def test_no_failure_response_carries_a_detail_key(envelope_client):
    """Plan §6: `detail` appears nowhere, so clients never special-case a shape."""
    responses = [envelope_client.get(f"/boom/{code}") for _, code, _ in EXPECTED_MAPPING]
    responses.append(envelope_client.post("/validate", json={}))

    for response in responses:
        assert "detail" not in response.json()
        assert set(response.json()) == {"error", "message"}


async def test_redeem_without_idempotency_key_is_422_in_the_envelope(client):
    """Issue #3 acceptance criterion, exercised against the real app.

    The header is declared required on the route, so its absence is a Pydantic
    validation failure — and must surface in the same two-key envelope as every
    other failure, not FastAPI's `{"detail": [...]}`.
    """
    response = await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"},
    )

    assert response.status_code == 422
    payload = response.json()
    assert set(payload) == {"error", "message"}
    assert payload["error"] == "VALIDATION_ERROR"
    assert "Idempotency-Key" in payload["message"]


async def test_empty_idempotency_key_is_rejected(client):
    """`min_length=1` on the header — a blank key is not a key."""
    response = await client.post(
        "/redeem",
        json={"code": "SAVE20", "customer_id": "cust-1", "order_id": "order-1"},
        headers={"Idempotency-Key": ""},
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}
