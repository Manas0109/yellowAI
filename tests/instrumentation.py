"""Live instrumentation for the acceptance run.

The acceptance scenarios finish in milliseconds and print a tidy summary, which
is not by itself evidence that anything was concurrent. This module wraps the
service entry points to record how many calls were genuinely in flight at once,
and streams progress while a burst runs.

Peak in-flight is the number that matters: if it were 1, the bursts would prove
nothing about contention, no matter how correct the totals looked.
"""

from __future__ import annotations

import time


class InFlightRecorder:
    """Counts concurrent calls through the service and reports as they land."""

    def __init__(self, label: str, expected: int, every: int = 25) -> None:
        self.label = label
        self.expected = expected
        self.every = every
        self.current = 0
        self.peak = 0
        self.started = 0
        self.completed = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.perf_counter()) - self.started_at

    def wrap(self, func):
        async def wrapped(*args, **kwargs):
            if self.started_at is None:
                self.started_at = time.perf_counter()
                print(f"\n  ▸ {self.label}: firing {self.expected} concurrent calls…")

            self.current += 1
            self.started += 1
            if self.current > self.peak:
                self.peak = self.current

            try:
                return await func(*args, **kwargs)
            finally:
                self.current -= 1
                self.completed += 1
                self.finished_at = time.perf_counter()
                self._tick()

        return wrapped

    def _tick(self) -> None:
        last = self.completed == self.expected
        if not last and self.completed % self.every:
            return

        filled = int(20 * self.completed / self.expected)
        bar = "█" * filled + "·" * (20 - filled)
        print(
            f"    [{self.elapsed * 1000:7.1f} ms] {bar} "
            f"{self.completed:>3}/{self.expected}  "
            f"in-flight:{self.current:>3}  peak:{self.peak:>3}"
        )

    def summary(self) -> list[tuple[str, object]]:
        return [
            ("Wall time", f"{self.elapsed * 1000:.1f} ms"),
            ("Calls entered service", self.started),
            ("Peak concurrent in service", self.peak),
            (
                "Overlap confirmed",
                "yes — requests genuinely interleaved"
                if self.peak > 1
                else "NO — calls ran one at a time",
            ),
        ]
