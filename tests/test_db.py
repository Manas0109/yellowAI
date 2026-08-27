"""Issue #2 acceptance criteria: pragmas, constraints, and the transaction helper."""

from __future__ import annotations

import sqlite3

import pytest

from app import db

COUPON = ("SAVE20", 100, 20.0, "2026-12-31T23:59:59+00:00", "STANDARD", 0, "2026-01-01T00:00:00+00:00")


async def seed_coupon(conn, code="SAVE20", coupon_type="STANDARD"):
    await conn.execute(
        "INSERT INTO coupons (code, max_redemptions, discount_percent, expires_at,"
        " type, redeemed_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (code, *COUPON[1:4], coupon_type, 0, COUPON[6]),
    )
    await conn.commit()


async def insert_redemption(conn, order_id, code="SAVE20", customer_id="cust-1",
                            coupon_type="STANDARD", status="ACTIVE"):
    await conn.execute(
        "INSERT INTO redemptions (order_id, code, customer_id, coupon_type, status,"
        " redeemed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, code, customer_id, coupon_type, status, COUPON[6]),
    )
    await conn.commit()


async def test_schema_is_created_and_wal_is_on(conn):
    async with conn.execute("PRAGMA journal_mode;") as cursor:
        assert (await cursor.fetchone())[0].lower() == "wal"

    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
    ) as cursor:
        names = {row[0] for row in await cursor.fetchall()}

    assert {"coupons", "redemptions", "idempotency_keys"} <= names
    assert "ux_standard_once_per_customer" in names


async def test_startup_is_idempotent(conn, tmp_path):
    # Reconnecting to the same file must not error on the IF NOT EXISTS DDL.
    path = str(tmp_path / "again.db")
    await db.disconnect()
    await db.connect(path)
    await db.disconnect()
    await db.connect(path)


@pytest.mark.parametrize(
    "column,value",
    [("max_redemptions", 0), ("discount_percent", 0), ("discount_percent", 101)],
)
async def test_check_constraints_reject_bad_coupons(conn, column, value):
    values = dict(
        zip(
            ["code", "max_redemptions", "discount_percent", "expires_at", "type",
             "redeemed_count", "created_at"],
            COUPON,
        )
    )
    values[column] = value
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute(
            "INSERT INTO coupons (code, max_redemptions, discount_percent, expires_at,"
            " type, redeemed_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(values.values()),
        )


async def test_redeemed_count_cannot_go_negative(conn):
    await seed_coupon(conn)
    with pytest.raises(sqlite3.IntegrityError):
        await conn.execute("UPDATE coupons SET redeemed_count = -1 WHERE code = 'SAVE20'")


async def test_order_id_is_unique(conn):
    await seed_coupon(conn)
    await insert_redemption(conn, "order-1")
    with pytest.raises(sqlite3.IntegrityError):
        await insert_redemption(conn, "order-1", customer_id="cust-2")


async def test_standard_is_once_per_customer_even_after_cancellation(conn):
    """The 'ever' ruling (plan §5), enforced by the partial index."""
    await seed_coupon(conn)
    await insert_redemption(conn, "order-1", status="CANCELLED")
    with pytest.raises(sqlite3.IntegrityError):
        await insert_redemption(conn, "order-2", status="ACTIVE")


async def test_stackable_allows_the_same_customer_repeatedly(conn):
    await seed_coupon(conn, code="REFER", coupon_type="STACKABLE")
    await insert_redemption(conn, "order-1", code="REFER", coupon_type="STACKABLE")
    await insert_redemption(conn, "order-2", code="REFER", coupon_type="STACKABLE")

    async with conn.execute("SELECT COUNT(*) FROM redemptions") as cursor:
        assert (await cursor.fetchone())[0] == 2


async def test_write_transaction_commits_on_success(conn):
    async with db.write_transaction() as tx:
        await tx.execute(
            "INSERT INTO coupons (code, max_redemptions, discount_percent, expires_at,"
            " type, redeemed_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            COUPON,
        )

    async with conn.execute("SELECT COUNT(*) FROM coupons") as cursor:
        assert (await cursor.fetchone())[0] == 1


async def test_write_transaction_rolls_back_on_exception(conn):
    with pytest.raises(RuntimeError):
        async with db.write_transaction() as tx:
            await tx.execute(
                "INSERT INTO coupons (code, max_redemptions, discount_percent,"
                " expires_at, type, redeemed_count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                COUPON,
            )
            raise RuntimeError("boom")

    async with conn.execute("SELECT COUNT(*) FROM coupons") as cursor:
        assert (await cursor.fetchone())[0] == 0


async def test_get_db_raises_before_lifespan_starts():
    await db.disconnect()
    with pytest.raises(RuntimeError):
        db.get_db()


@pytest.mark.parametrize(
    "pragma,expected",
    [("foreign_keys", 1), ("synchronous", 1), ("busy_timeout", 5000)],
)
async def test_remaining_pragmas_are_applied(conn, pragma, expected):
    """`synchronous` 1 is NORMAL — the durable-enough companion setting for WAL."""
    async with conn.execute(f"PRAGMA {pragma};") as cursor:
        assert (await cursor.fetchone())[0] == expected


async def test_row_factory_gives_mapping_access(conn):
    await seed_coupon(conn)
    async with conn.execute("SELECT code, max_redemptions FROM coupons") as cursor:
        row = await cursor.fetchone()
    assert row["code"] == "SAVE20"
    assert row["max_redemptions"] == COUPON[1]


async def test_coupon_type_must_be_a_known_enum_value(conn):
    with pytest.raises(sqlite3.IntegrityError):
        await seed_coupon(conn, coupon_type="GIFT")


async def test_coupon_boundary_values_are_accepted(conn):
    """The CHECKs are `> 0` and `<= 100`, so 1 and 100.0 must both insert."""
    await conn.execute(
        "INSERT INTO coupons (code, max_redemptions, discount_percent, expires_at,"
        " type, redeemed_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("EDGE", 1, 100.0, COUPON[3], "STACKABLE", 0, COUPON[6]),
    )
    await conn.commit()


@pytest.mark.parametrize(
    "coupon_type,status",
    [("STANDARD", "REFUNDED"), ("GIFT", "ACTIVE")],
)
async def test_redemption_check_constraints_reject_bad_values(conn, coupon_type, status):
    await seed_coupon(conn)
    with pytest.raises(sqlite3.IntegrityError):
        await insert_redemption(conn, "order-1", coupon_type=coupon_type, status=status)


async def test_foreign_key_to_coupons_is_enforced(conn):
    """Proves `PRAGMA foreign_keys=ON` took effect — SQLite ignores FKs without it."""
    with pytest.raises(sqlite3.IntegrityError):
        await insert_redemption(conn, "order-1", code="NO-SUCH-CODE")


async def test_disconnect_closes_the_connection(conn):
    await db.disconnect()
    with pytest.raises(ValueError):  # aiosqlite: no active connection
        await conn.execute("SELECT 1")
