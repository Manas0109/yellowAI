"""Issues #6 and #7: the seed endpoint and the counter read."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import NOW, redeem, seed_coupon


async def test_create_returns_201_with_the_full_response(client):
    body = await seed_coupon(client, max_redemptions=50)
    assert body == {
        "code": "SAVE20",
        "max_redemptions": 50,
        "redeemed_count": 0,
        "remaining": 50,
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
        "type": "STANDARD",
    }


async def test_expiry_is_stored_normalised_to_utc(client, conn):
    await seed_coupon(client, expires_at=None, code="TZ")
    response = await client.post(
        "/coupons",
        json={
            "code": "OFFSET",
            "max_redemptions": 5,
            "discount_percent": 10,
            "expires_at": "2026-06-01T09:00:00+05:30",
            "type": "STANDARD",
        },
    )
    assert response.json()["expires_at"] == "2026-06-01T03:30:00+00:00"

    async with conn.execute("SELECT expires_at FROM coupons WHERE code = 'OFFSET'") as c:
        assert (await c.fetchone())[0] == "2026-06-01T03:30:00+00:00"


async def test_naive_expiry_is_treated_as_utc(client):
    response = await client.post(
        "/coupons",
        json={
            "code": "NAIVE",
            "max_redemptions": 5,
            "discount_percent": 10,
            "expires_at": "2026-06-01T09:00:00",
            "type": "STANDARD",
        },
    )
    assert response.json()["expires_at"] == "2026-06-01T09:00:00+00:00"


async def test_duplicate_code_is_rejected(client):
    await seed_coupon(client)
    response = await client.post(
        "/coupons",
        json={
            "code": "SAVE20",
            "max_redemptions": 1,
            "discount_percent": 99,
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "type": "STACKABLE",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == "CODE_ALREADY_EXISTS"


async def test_duplicate_attempt_leaves_the_original_row_untouched(client, conn):
    """A re-seed must never reset redeemed_count — that would blow the cap."""
    await seed_coupon(client, max_redemptions=10)
    for n in range(3):
        await redeem(client, customer_id=f"cust-{n}", order_id=f"order-{n}")

    await client.post(
        "/coupons",
        json={
            "code": "SAVE20",
            "max_redemptions": 999,
            "discount_percent": 99,
            "expires_at": (NOW + timedelta(days=1)).isoformat(),
            "type": "STACKABLE",
        },
    )

    async with conn.execute(
        "SELECT max_redemptions, discount_percent, redeemed_count, type"
        " FROM coupons WHERE code = 'SAVE20'"
    ) as cursor:
        row = await cursor.fetchone()
    assert tuple(row) == (10, 20.0, 3, "STANDARD")


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_redemptions": 0},
        {"max_redemptions": -1},
        {"discount_percent": 0},
        {"discount_percent": 101},
        {"type": "MYSTERY"},
        {"expires_at": "not-a-date"},
        {"code": ""},
    ],
)
async def test_invalid_bodies_return_the_envelope(client, overrides):
    payload = {
        "code": "BAD",
        "max_redemptions": 5,
        "discount_percent": 10,
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "type": "STANDARD",
    }
    payload.update(overrides)
    response = await client.post("/coupons", json=payload)

    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}


@pytest.mark.parametrize("coupon_type", ["STANDARD", "STACKABLE"])
async def test_both_types_can_be_created(client, coupon_type):
    body = await seed_coupon(client, code=f"C-{coupon_type}", type=coupon_type)
    assert body["type"] == coupon_type


async def test_get_returns_exactly_four_keys(client):
    await seed_coupon(client, max_redemptions=50)
    response = await client.get("/coupons/SAVE20")

    assert response.status_code == 200
    assert response.json() == {
        "code": "SAVE20",
        "redeemed_count": 0,
        "remaining": 50,
        "max_redemptions": 50,
    }


async def test_get_unknown_code_is_404(client):
    response = await client.get("/coupons/NOPE")
    assert response.status_code == 404
    assert response.json()["error"] == "UNKNOWN_CODE"


async def test_get_does_not_take_the_write_lock(client):
    """The read must not serialise behind in-flight checkouts."""
    from app.service import write_lock

    await seed_coupon(client)
    async with write_lock:
        response = await client.get("/coupons/SAVE20")
    assert response.status_code == 200
