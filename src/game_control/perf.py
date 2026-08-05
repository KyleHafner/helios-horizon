"""In-memory performance measurements for the slot daemon."""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any


FLUSH_INTERVAL = timedelta(minutes=5)


class _Window:
    def __init__(self, *, maxlen: int = 1024):
        self.samples: deque[float] = deque(maxlen=maxlen)
        self.interval_samples: deque[float] = deque(maxlen=maxlen)
        self.interval_count = 0
        self.interval_total = 0.0
        self.interval_max = 0.0

    def record(self, value: float) -> None:
        value = max(0.0, float(value))
        self.samples.append(value)
        self.interval_samples.append(value)
        self.interval_count += 1
        self.interval_total += value
        self.interval_max = max(self.interval_max, value)

    def snapshot(self) -> dict[str, float | int | None]:
        return _aggregate(self.samples)

    def interval_snapshot(self) -> dict[str, float | int | None]:
        values = self.interval_samples
        if not values:
            return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
        ordered = sorted(values)
        return {
            "count": self.interval_count,
            "avg_ms": self.interval_total / self.interval_count,
            "p95_ms": _percentile(ordered, 0.95),
            "max_ms": self.interval_max,
        }

    def reset_interval(self) -> None:
        self.interval_samples.clear()
        self.interval_count = 0
        self.interval_total = 0.0
        self.interval_max = 0.0


def _aggregate(values: Any) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "count": len(ordered),
        "avg_ms": sum(ordered) / len(ordered),
        "p95_ms": _percentile(ordered, 0.95),
        "max_ms": ordered[-1],
    }


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


class PerformanceTracker:
    """O(1) append-only collection with on-demand percentile calculation."""

    def __init__(self, *, maxlen: int = 1024):
        if not 1 <= maxlen <= 65536:
            raise ValueError("performance ring bound out of range")
        self.cycle = _Window(maxlen=maxlen)
        self.rpc = _Window(maxlen=maxlen)
        self._last_flush: datetime | None = None

    def record_cycle(self, duration_ms: float) -> None:
        self.cycle.record(duration_ms)

    def record_rpc(self, duration_ms: float) -> None:
        self.rpc.record(duration_ms)

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        return {"cycle": self.cycle.snapshot(), "rpc": self.rpc.snapshot()}

    def flush_if_due(
        self,
        connection: sqlite3.Connection,
        *,
        now: datetime,
        profile_id: str = "slotd",
    ) -> int:
        now = _aware(now)
        if self._last_flush is None:
            self._last_flush = now
            return 0
        if now - self._last_flush < FLUSH_INTERVAL:
            return 0
        aggregates = {
            "perf.cycle_avg_ms": self.cycle.interval_snapshot()["avg_ms"],
            "perf.cycle_p95_ms": self.cycle.interval_snapshot()["p95_ms"],
            "perf.cycle_max_ms": self.cycle.interval_snapshot()["max_ms"],
            "perf.rpc_avg_ms": self.rpc.interval_snapshot()["avg_ms"],
            "perf.rpc_p95_ms": self.rpc.interval_snapshot()["p95_ms"],
            "perf.rpc_max_ms": self.rpc.interval_snapshot()["max_ms"],
        }
        rows = [
            (profile_id, metric, _iso(now), float(value))
            for metric, value in aggregates.items()
            if value is not None
        ]
        if rows:
            with connection:
                connection.executemany(
                    "INSERT INTO metric_samples(profile_id, metric, ts, value) VALUES (?, ?, ?, ?)",
                    rows,
                )
        self.cycle.reset_interval()
        self.rpc.reset_interval()
        self._last_flush = now
        return len(rows)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["FLUSH_INTERVAL", "PerformanceTracker"]
