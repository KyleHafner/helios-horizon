"""A small, slotd-owned scheduler for periodic telemetry collection.

The sampler deliberately knows nothing about profiles, adapters, or storage.  A
caller supplies one callback and owns the result.  Its clock is injectable so
cadence and overrun behaviour can be tested without waiting in real time.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any


class SamplerInvariantError(RuntimeError):
    """An invariant failure that must terminate collection."""


SampleOnce = Callable[[], Any | Awaitable[Any]]


class TelemetrySampler:
    """Run ``sample_once`` on a fixed monotonic schedule.

    The first cycle runs immediately.  Subsequent deadlines are derived from
    the original schedule, rather than from completion time.  If a cycle
    overruns one or more deadlines, those deadlines are counted and coalesced;
    the sampler never bursts to catch up.
    """

    def __init__(
        self,
        sample_once: SampleOnce,
        *,
        interval_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        duration_capacity: int = 64,
    ) -> None:
        if not callable(sample_once):
            raise TypeError("sample_once must be callable")
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive and finite")
        if duration_capacity < 1:
            raise ValueError("duration_capacity must be positive")
        self.sample_once = sample_once
        self.interval_seconds = float(interval_seconds)
        self._monotonic = monotonic
        self._sleep = sleep
        self._durations_ms: deque[float] = deque(maxlen=int(duration_capacity))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._closed = False
        self._started_at: float | None = None
        self._last_cycle_at: float | None = None
        self._cycles = 0
        self._successes = 0
        self._failures = 0
        self._consecutive_failures = 0
        self._missed_deadlines = 0
        self._last_error: str | None = None

    def start(self) -> asyncio.Task[None]:
        """Start a supervised task and return it; repeated starts are rejected."""
        if self._task is not None and not self._task.done():
            raise RuntimeError("telemetry sampler is already running")
        if self._closed:
            raise RuntimeError("telemetry sampler is shut down")
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="horizon-telemetry-sampler")
        return self._task

    async def run(self) -> None:
        """Run until :meth:`shutdown` is requested or an invariant fails."""
        if self._running:
            raise RuntimeError("telemetry sampler is already running")
        self._running = True
        self._started_at = self._monotonic()
        deadline = self._started_at
        try:
            while not self._stop.is_set():
                await self._cycle()
                if self._stop.is_set():
                    break
                deadline += self.interval_seconds
                now = self._monotonic()
                if now >= deadline:
                    missed = int((now - deadline) // self.interval_seconds) + 1
                    self._missed_deadlines += missed
                    deadline += missed * self.interval_seconds
                    now = self._monotonic()
                await self._sleep(deadline - now)
        finally:
            self._running = False
            self._closed = True

    async def _cycle(self) -> None:
        started = self._monotonic()
        try:
            result = self.sample_once()
            if inspect.isawaitable(result):
                await result
        except SamplerInvariantError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # ordinary collection failures are bounded
            self._failures += 1
            self._consecutive_failures += 1
            self._last_error = type(exc).__name__
        else:
            self._successes += 1
            self._consecutive_failures = 0
            self._last_error = None
        finally:
            finished = self._monotonic()
            duration_ms = max(0.0, (finished - started) * 1000.0)
            self._durations_ms.append(duration_ms)
            self._cycles += 1
            self._last_cycle_at = finished

    async def shutdown(self) -> None:
        """Request shutdown and await a started task, if present."""
        self._stop.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def wait_closed(self) -> None:
        task = self._task
        if task is not None:
            await task

    def health(self, *, now: float | None = None) -> dict[str, Any]:
        """Return bounded scheduler health suitable for status/telemetry export."""
        current = self._monotonic() if now is None else now
        durations = tuple(self._durations_ms)
        ordered = sorted(durations)
        p95 = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)] if ordered else None
        return {
            "running": self._running,
            "closed": self._closed,
            "interval_seconds": self.interval_seconds,
            "cycles": self._cycles,
            "successes": self._successes,
            "failures": self._failures,
            "consecutive_failures": self._consecutive_failures,
            "missed_deadlines": self._missed_deadlines,
            "last_error": self._last_error,
            "last_cycle_age_ms": None if self._last_cycle_at is None else max(0.0, (current - self._last_cycle_at) * 1000.0),
            "last_cycle_duration_ms": durations[-1] if durations else None,
            "cycle_duration_p95_ms": p95,
            "cycle_duration_mean_ms": statistics.fmean(durations) if durations else None,
            "cycle_duration_samples": len(durations),
        }


# A concise name for callers that do not need the more specific type name.
Sampler = TelemetrySampler
