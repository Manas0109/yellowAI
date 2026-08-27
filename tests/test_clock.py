"""Issue #4 acceptance criteria."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import clock as clock_module
from app.clock import FrozenClock, SystemClock, isoformat_utc, parse_utc, to_utc


def test_system_clock_is_utc_aware():
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_frozen_clock_returns_the_pinned_instant_unchanged():
    instant = datetime(2026, 6, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    clock = FrozenClock(instant)
    assert clock.now() == instant
    assert clock.now() == instant
    assert clock.now().microsecond == 123456


def test_frozen_clock_can_advance_by_a_microsecond():
    instant = datetime(2026, 6, 1, tzinfo=timezone.utc)
    clock = FrozenClock(instant)
    clock.advance(microseconds=1)
    assert clock.now() == instant + timedelta(microseconds=1)


def test_clock_can_be_swapped_app_wide_and_restored():
    instant = datetime(2030, 1, 1, tzinfo=timezone.utc)
    try:
        clock_module.set_clock(FrozenClock(instant))
        assert clock_module.get_clock().now() == instant
    finally:
        clock_module.reset_clock()
    assert isinstance(clock_module.get_clock(), SystemClock)


def test_naive_datetime_is_assumed_utc():
    assert to_utc(datetime(2026, 1, 1, 9, 0)) == datetime(
        2026, 1, 1, 9, 0, tzinfo=timezone.utc
    )


def test_offset_bearing_string_is_converted_to_utc():
    assert parse_utc("2026-01-01T09:00:00+05:30") == datetime(
        2026, 1, 1, 3, 30, tzinfo=timezone.utc
    )


def test_trailing_z_is_accepted():
    assert parse_utc("2026-01-01T09:00:00Z") == datetime(
        2026, 1, 1, 9, 0, tzinfo=timezone.utc
    )


def test_naive_string_is_treated_as_utc():
    assert parse_utc("2026-01-01T09:00:00") == datetime(
        2026, 1, 1, 9, 0, tzinfo=timezone.utc
    )


def test_isoformat_round_trips():
    instant = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert parse_utc(isoformat_utc(instant)) == instant
