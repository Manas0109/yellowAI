"""Failure taxonomy (plan §6).

Every failure leaves this service as the same two-key envelope::

    { "error": "NO_REDEMPTIONS_LEFT", "message": "Coupon SAVE20 is fully redeemed." }

Clients branch on ``error``, never on the HTTP status. The status is still
meaningful — it carries the retryable/permanent signal — but it is redundant
with the code, deliberately.

The ordering in which these are *raised* matters as much as the codes
themselves; see ``ERROR_PRECEDENCE`` below and plan §4.
"""

from __future__ import annotations


class CouponError(Exception):
    """Base class for every expected failure mode.

    Subclasses declare ``code`` and ``http_status``; instances carry a
    human-readable ``message`` naming the identifier that caused the problem.
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownCode(CouponError):
    code = "UNKNOWN_CODE"
    http_status = 404

    def __init__(self, coupon_code: str) -> None:
        super().__init__(f"Coupon {coupon_code} does not exist.")


class CouponExpired(CouponError):
    code = "COUPON_EXPIRED"
    http_status = 410

    def __init__(self, coupon_code: str) -> None:
        super().__init__(f"Coupon {coupon_code} has expired.")


class CustomerAlreadyRedeemed(CouponError):
    code = "CUSTOMER_ALREADY_REDEEMED"
    http_status = 409

    def __init__(self, coupon_code: str, customer_id: str) -> None:
        super().__init__(
            f"Customer {customer_id} has already redeemed coupon {coupon_code}. "
            f"A STANDARD coupon may be redeemed once per customer, ever — "
            f"cancelling the order returns the redemption slot but does not "
            f"restore the customer's eligibility."
        )


class OrderAlreadyHasRedemption(CouponError):
    code = "ORDER_ALREADY_HAS_REDEMPTION"
    http_status = 409

    def __init__(self, order_id: str) -> None:
        super().__init__(f"Order {order_id} already has a coupon redemption.")


class NoRedemptionsLeft(CouponError):
    code = "NO_REDEMPTIONS_LEFT"
    http_status = 409

    def __init__(self, coupon_code: str) -> None:
        super().__init__(f"Coupon {coupon_code} is fully redeemed.")


class CodeAlreadyExists(CouponError):
    code = "CODE_ALREADY_EXISTS"
    http_status = 409

    def __init__(self, coupon_code: str) -> None:
        super().__init__(
            f"Coupon {coupon_code} already exists. Coupon codes are immutable: "
            f"re-seeding would reset redeemed_count and break the redemption cap."
        )


class IdempotencyKeyReuse(CouponError):
    code = "IDEMPOTENCY_KEY_REUSE"
    http_status = 422

    def __init__(self, key: str) -> None:
        super().__init__(
            f"Idempotency-Key {key} was already used with a different request "
            f"body. Reusing a key across different requests is a client bug."
        )


class ValidationFailed(CouponError):
    """Request-shape failure — Pydantic, or a missing required header."""

    code = "VALIDATION_ERROR"
    http_status = 422


#: The order in which redeem() evaluates failure conditions (plan §4).
#: Permanent conditions are reported before transient ones, so a client is
#: never told to retry a coupon that is expired.
ERROR_PRECEDENCE: tuple[str, ...] = (
    UnknownCode.code,
    CouponExpired.code,
    CustomerAlreadyRedeemed.code,
    OrderAlreadyHasRedemption.code,
    NoRedemptionsLeft.code,
)
