"""Issue #4 acceptance criteria."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


# --- Additional coverage: boundary cases, the injection point, normalisation ---


def test_frozen_clock_covers_the_expiry_boundary_triple():
    """Plan §8 item 4: expires_at - 1µs, exactly expires_at, +1µs."""
    expires_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    clock = FrozenClock(expires_at - timedelta(microseconds=1))
    assert clock.now() < expires_at

    clock.advance(microseconds=1)
    assert clock.now() == expires_at
    assert not clock.now() < expires_at

    clock.advance(microseconds=1)
    assert clock.now() > expires_at


def test_frozen_clock_accepts_an_iso_string():
    clock = FrozenClock("2026-06-01T17:30:00.000001+05:30")
    assert clock.now() == datetime(2026, 6, 1, 12, 0, 0, 1, tzinfo=timezone.utc)


def test_frozen_clock_set_re_pins_and_returns_the_instant():
    clock = FrozenClock("2026-06-01T00:00:00Z")
    returned = clock.set("2026-07-01T00:00:00Z")
    assert returned == clock.now() == datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_frozen_clock_advances_by_timedelta_or_seconds():
    clock = FrozenClock("2026-06-01T00:00:00Z")
    assert clock.advance(timedelta(microseconds=1)) == datetime(
        2026, 6, 1, 0, 0, 0, 1, tzinfo=timezone.utc
    )
    assert clock.advance(-1.5) == datetime(2026, 5, 31, 23, 59, 58, 500001, tzinfo=timezone.utc)


def test_frozen_clock_rejects_a_delta_and_keywords_together():
    clock = FrozenClock("2026-06-01T00:00:00Z")
    with pytest.raises(TypeError):
        clock.advance(timedelta(seconds=1), microseconds=1)


def test_set_clock_returns_the_clock_it_replaced():
    frozen = FrozenClock("2026-06-01T00:00:00Z")
    try:
        previous = clock_module.set_clock(frozen)
        assert isinstance(previous, SystemClock)
        assert clock_module.get_clock() is frozen
    finally:
        clock_module.reset_clock()


def test_using_clock_installs_and_restores():
    frozen = FrozenClock("2026-06-01T00:00:00Z")
    with clock_module.using_clock(frozen) as installed:
        assert installed is frozen
        assert clock_module.get_clock() is frozen
    assert isinstance(clock_module.get_clock(), SystemClock)


def test_using_clock_restores_even_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with clock_module.using_clock(FrozenClock("2026-06-01T00:00:00Z")):
            raise RuntimeError("boom")
    assert isinstance(clock_module.get_clock(), SystemClock)


def test_to_utc_accepts_a_string_as_well_as_a_datetime():
    assert to_utc("2026-01-01T09:00:00+05:30") == datetime(
        2026, 1, 1, 3, 30, tzinfo=timezone.utc
    )


def test_to_utc_is_idempotent_and_always_tagged_utc():
    once = to_utc("2026-01-01T09:00:00+05:30")
    assert to_utc(once) == once
    for value in ("2026-01-01T09:00:00", "2026-01-01T09:00:00+05:30", datetime(2026, 1, 1)):
        assert to_utc(value).tzinfo is timezone.utc


def test_microseconds_survive_normalisation():
    assert parse_utc("2026-01-01T09:00:00.123456+00:00").microsecond == 123456


@pytest.mark.parametrize("value", ["", "   ", "not-a-timestamp", "2026-13-01T00:00:00Z"])
def test_an_unparseable_timestamp_raises_value_error(value):
    with pytest.raises(ValueError):
        parse_utc(value)


@pytest.mark.parametrize("value", [None, 1740787200, 3.14, object()])
def test_to_utc_rejects_the_wrong_type(value):
    with pytest.raises(TypeError):
        to_utc(value)  # type: ignore[arg-type]
