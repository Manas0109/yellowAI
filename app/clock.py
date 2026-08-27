"""Injectable, UTC-aware clock.

Contract (plan §3, "Clock discipline"): the redeem/cancel request path calls
``clock.now()`` **exactly once**, inside the lock, at the top of the
transaction, and threads that single value through every check. No other
``datetime.now()`` may appear anywhere in that path — otherwise two checks in
the same request could straddle the expiry instant and resolve inconsistently.

Tests swap the clock app-wide via :func:`set_clock` (or the :func:`using_clock`
context manager) so they can pin the exact expiry microsecond.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol, Union

Instant = Union[datetime, str]


class Clock(Protocol):
    """Anything that can tell us the current instant in UTC."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The real clock. Used everywhere outside tests."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SystemClock()"


class FrozenClock:
    """A clock pinned to a fixed instant, for tests.

    The pinned value is returned unchanged on every call, preserving
    microsecond precision so expiry-boundary cases can be tested exactly.
    """

    def __init__(self, instant: Instant) -> None:
        self._instant = to_utc(instant)

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: Instant) -> datetime:
        """Re-pin the clock to a new instant. Returns the new instant."""
        self._instant = to_utc(instant)
        return self._instant

    def advance(self, delta: Union[timedelta, int, float, None] = None, **timedelta_kwargs) -> datetime:
        """Move the pinned instant, e.g. ``advance(microseconds=1)``.

        Also accepts a ``timedelta`` or a number of seconds positionally.
        Negative values move the clock backwards. Returns the new instant.
        """
        if delta is None:
            delta = timedelta(**timedelta_kwargs)
        elif timedelta_kwargs:
            raise TypeError("pass either a delta or timedelta keywords, not both")
        elif not isinstance(delta, timedelta):
            delta = timedelta(seconds=delta)
        self._instant = self._instant + delta
        return self._instant

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FrozenClock({self._instant.isoformat()})"


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """Return the clock the app is currently using."""
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Swap the app-wide clock. Tests use this; production never calls it.

    Returns the clock that was replaced, so a caller can restore it.
    """
    global _clock
    previous, _clock = _clock, clock
    return previous


def reset_clock() -> None:
    """Restore the real clock."""
    set_clock(SystemClock())


@contextmanager
def using_clock(clock: Clock) -> Iterator[Clock]:
    """Install ``clock`` for the duration of the block, then restore the previous one."""
    previous = set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)


def to_utc(value: Instant) -> datetime:
    """Normalise a datetime — or an ISO-8601 string — to timezone-aware UTC.

    A naive value is *assumed* to already be UTC rather than local time —
    stating the assumption explicitly beats silently picking up whatever
    timezone the host happens to be in. Strings are parsed by :func:`parse_utc`.
    """
    if isinstance(value, str):
        return parse_utc(value)
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime or ISO-8601 str, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime.

    Accepts a trailing ``Z``, which :meth:`datetime.fromisoformat` only learned
    to handle in 3.11 and which we normalise anyway for older-style inputs.
    """
    text = value.strip()
    if not text:
        raise ValueError("timestamp string is empty")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    return to_utc(parsed)


def isoformat_utc(value: Instant) -> str:
    """Render a datetime as an ISO-8601 UTC string, for storage and responses."""
    return to_utc(value).isoformat()
