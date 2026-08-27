"""Shared fixtures.

A temp-file database per test — not ``:memory:``, so the tests exercise the
same WAL-mode file backend the service actually runs on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app import clock as clock_module
from app import db


@pytest_asyncio.fixture
async def conn(tmp_path):
    """A connected, schema-created database scoped to one test."""
    connection = await db.connect(str(tmp_path / "test.db"))
    try:
        yield connection
    finally:
        await db.disconnect()


@pytest.fixture
def frozen_clock():
    """Pin the app-wide clock, and restore the real one afterwards."""
    frozen = clock_module.FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    clock_module.set_clock(frozen)
    yield frozen
    clock_module.reset_clock()
