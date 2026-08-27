"""Persistence layer (plan §2, §3).

One shared ``aiosqlite`` connection, opened in the FastAPI lifespan. Single
process, single worker, single connection — the concurrency argument in plan §3
depends on that, and it is stated again in the README.

Schema is created with ``CREATE TABLE IF NOT EXISTS`` at startup; plan §10
explicitly rules migrations out of the MVP.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

DEFAULT_DB_PATH = "coupons.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS coupons (
    code             TEXT    PRIMARY KEY,
    max_redemptions  INTEGER NOT NULL CHECK (max_redemptions > 0),
    discount_percent REAL    NOT NULL CHECK (discount_percent > 0 AND discount_percent <= 100),
    expires_at       TEXT    NOT NULL,
    type             TEXT    NOT NULL CHECK (type IN ('STANDARD', 'STACKABLE')),
    redeemed_count   INTEGER NOT NULL DEFAULT 0 CHECK (redeemed_count >= 0),
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS redemptions (
    id           INTEGER PRIMARY KEY,
    order_id     TEXT    NOT NULL UNIQUE,
    code         TEXT    NOT NULL REFERENCES coupons(code),
    customer_id  TEXT    NOT NULL,
    coupon_type  TEXT    NOT NULL CHECK (coupon_type IN ('STANDARD', 'STACKABLE')),
    status       TEXT    NOT NULL CHECK (status IN ('ACTIVE', 'CANCELLED')),
    redeemed_at  TEXT    NOT NULL,
    cancelled_at TEXT
);

-- The per-customer rule for STANDARD coupons, enforced by the database.
-- Note the absence of a status predicate: the index spans CANCELLED rows too,
-- which is what makes "once per customer, ever" hold across a cancellation
-- (plan §5). Cancelling returns the global slot; it does not return the
-- customer's eligibility.
CREATE UNIQUE INDEX IF NOT EXISTS ux_standard_once_per_customer
    ON redemptions(code, customer_id)
    WHERE coupon_type = 'STANDARD';

CREATE INDEX IF NOT EXISTS ix_redemptions_code_customer
    ON redemptions(code, customer_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           TEXT PRIMARY KEY,
    request_hash  TEXT NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
"""

_connection: aiosqlite.Connection | None = None


def db_path() -> str:
    return os.environ.get("COUPONS_DB_PATH", DEFAULT_DB_PATH)


async def connect(path: str | None = None) -> aiosqlite.Connection:
    """Open the shared connection, apply pragmas, and create the schema."""
    global _connection
    conn = await aiosqlite.connect(path or db_path())
    conn.row_factory = aiosqlite.Row

    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA synchronous=NORMAL;")
    await conn.execute("PRAGMA busy_timeout=5000;")

    await conn.executescript(SCHEMA)
    await conn.commit()

    _connection = conn
    return conn


async def disconnect() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def get_db() -> aiosqlite.Connection:
    """Return the shared connection. Raises if the lifespan has not run."""
    if _connection is None:
        raise RuntimeError("Database is not connected; app lifespan has not started.")
    return _connection


@asynccontextmanager
async def write_transaction() -> AsyncIterator[aiosqlite.Connection]:
    """Run a write under ``BEGIN IMMEDIATE``.

    ``BEGIN IMMEDIATE`` takes the write lock up front rather than upgrading a
    read lock mid-transaction, so a read-then-write sequence cannot lose its
    place to another writer.

    Note this cannot nest: every request shares one connection, so the asyncio
    lock in ``service.py`` is what keeps two transactions from overlapping here.
    This matters if the connection ever becomes a pool — then these guards
    start doing real work, and the lock stops being sufficient on its own.
    """
    conn = get_db()
    await conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
    except BaseException:
        await conn.rollback()
        raise
    else:
        await conn.commit()
