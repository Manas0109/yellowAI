You are building a coupon redemption service for an e-commerce checkout.

Each coupon has: code, max_redemptions (total times it can ever be used, ever), discount_percent, expires_at, and type (STANDARD or STACKABLE). Rules:

A coupon cannot be redeemed more than max_redemptions times — even under a burst of simultaneous checkouts on a flash-sale coupon.
A STANDARD coupon can only be redeemed once per customer_id, ever.
A STACKABLE coupon (e.g. referral codes) has no per-customer limit, but still respects max_redemptions globally.
A coupon redeemed after expires_at 
must be rejected — but a redemption in flight at the exact expiry instant must resolve consistently, not by luck of thread scheduling.
Orders can be cancelled. Canceling an order that uses a coupon must give the redemption slot back (redeemed_count decrements) — but only once per order, even if the cancellation endpoint is called twice.
Retrying the same redemption request (network client retries after a timeout, doesn't know if the first one landed) must not double-redeem. Callers send an Idempotency-Key header on POST /redeem.

API:

POST /coupons → { code, max_redemptions, discount_percent, expires_at, type } — seed data.
POST /redeem (header: Idempotency-Key) → { code, customer_id, order_id } → { success: true, remaining } or a clear, distinct error per failure mode (already used / no redemptions left / expired / unknown code / idempotency replay).
POST /orders/:order_id/cancel → reverses the coupon redemption tied to that order, if any. Calling it twice must be a no-op the second time, not a double refund of the slot.
GET /coupons/:code → { redeemed_count, remaining, max_redemptions } — this number must be correct at all times, not eventually correct.
