"""Injectable, UTC-aware clock.

Contract (plan §3, "Clock discipline"): the redeem/cancel request path calls
``clock.now()`` **exactly once**, inside the lock, at the top of the
transaction, and threads that single value through every check. No other
``datetime.now()`` may appear anywhere in that path — otherwise two checks in
the same request could straddle the expiry instant and resolve inconsistently.

Tests swap the clock app-wide via :func:`set_clock` so they can pin the exact
expiry microsecond.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Protocol


class Clock(Protocol):
    """Anything that can tell us the current instant in UTC."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. Used everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """A clock pinned to a fixed instant, for tests.

    The pinned value is returned unchanged on every call, preserving
    microsecond precision so expiry-boundary cases can be tested exactly.
    """

    def __init__(self, instant: datetime) -> None:
        self._instant = to_utc(instant)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        """Re-pin the clock to a new instant."""
        self._instant = to_utc(instant)

    def advance(self, **timedelta_kwargs) -> None:
        """Move the pinned instant forward, e.g. ``advance(microseconds=1)``."""
        from datetime import timedelta

        self._instant = self._instant + timedelta(**timedelta_kwargs)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """Return the clock the app is currently using."""
    return _clock


def set_clock(clock: Clock) -> None:
    """Swap the app-wide clock. Tests use this; production never calls it."""
    global _clock
    _clock = clock


def reset_clock() -> None:
    """Restore the real clock."""
    set_clock(SystemClock())


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the duration of the block, then restore.

    Convenience over :func:`set_clock` for tests that need a pinned instant in
    one scope only; the previous clock comes back even if the block raises.
    """
    previous = get_clock()
    set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


def to_utc(value: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC.

    A naive datetime is *assumed* to already be UTC rather than local time —
    stating the assumption explicitly beats silently picking up whatever
    timezone the host happens to be in.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime.

    Accepts a trailing ``Z``, which :meth:`datetime.fromisoformat` only learned
    to handle in 3.11 and which we normalise anyway for older-style inputs.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return to_utc(datetime.fromisoformat(text))


def isoformat_utc(value: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string, for storage and responses."""
    return to_utc(value).isoformat()
