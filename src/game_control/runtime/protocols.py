"""Dependency-light interfaces shared by the runtime extraction.

These protocols deliberately know neither the controller nor the concrete
telemetry/alert implementations.  They are the stable seam used while the
composition root is moved in a later, sequential commit.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


AlertProfileState = Literal["starting", "running", "stopped", "stopping", "failed"]
_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_:.-]{0,127}$")


def _is_safe_identifier(value: Any) -> bool:
    """Return whether a value satisfies the bounded ASCII identifier contract."""

    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _bounded_nonnegative(value: Any, *, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return number


@dataclass(frozen=True, slots=True)
class AlertObservation:
    """A bounded observation accepted by the slotd-owned alert sink."""

    profile_id: str
    profile_state: AlertProfileState
    now: float
    mspt_p95: float | None = None
    rss_bytes: float | None = None
    wake_duration_ms: float | None = None
    benchmark_regression: bool | None = None

    def __post_init__(self) -> None:
        if not _is_safe_identifier(self.profile_id):
            raise ValueError("invalid alert profile")
        if not isinstance(self.profile_state, str) or self.profile_state not in {
            "starting", "running", "stopped", "stopping", "failed"
        }:
            raise ValueError("invalid alert profile state")
        if (
            isinstance(self.now, bool)
            or not isinstance(self.now, (int, float))
            or not math.isfinite(float(self.now))
            or float(self.now) < 0
        ):
            raise ValueError("invalid alert timestamp")
        for value, name in (
            (self.mspt_p95, "mspt_p95"),
            (self.rss_bytes, "rss_bytes"),
            (self.wake_duration_ms, "wake_duration_ms"),
        ):
            _bounded_nonnegative(value, name=name)
        if self.benchmark_regression is not None and not isinstance(self.benchmark_regression, bool):
            raise ValueError("benchmark_regression must be boolean or None")


@runtime_checkable
class AlertSink(Protocol):
    """Neutral sink used by telemetry sources to submit bounded observations."""

    def observe(self, observation: AlertObservation) -> None:
        """Accept one typed observation without exposing arbitrary event names."""

    async def close(self) -> None:
        """Stop intake and drain the sink's owned delivery tasks."""


@runtime_checkable
class StatusSnapshotProvider(Protocol):
    """The status seam consumed by the future single telemetry cycle."""

    async def snapshot(self, *, persist: bool = False, force: bool = False) -> Any:
        """Return one status projection; implementations define its record type."""


@runtime_checkable
class TelemetryCollector(Protocol):
    """Generic telemetry collection, excluding process-resource persistence."""

    async def collect(self, snapshot: Any) -> None:
        """Collect generic host/tick/RCON/GC/log values for one snapshot."""

    async def close(self) -> None:
        """Stop source intake and release only resources owned by the collector."""

    def health(self) -> Mapping[str, Any]:
        """Return bounded, secret-free health information."""


@runtime_checkable
class TelemetryDatabaseWriter(Protocol):
    """The only persistence seam the runtime may use for telemetry samples."""

    def enqueue_sample(
        self,
        profile_id: Any,
        metric: str,
        value: float | int | None,
        *,
        ts_ms: int,
        state: str = "available",
        labels: Mapping[str, str] | None = None,
    ) -> bool:
        """Queue one generic sample without blocking the event loop."""

    def enqueue_process_sample(self, profile_id: Any, sample: Any | None, *, ts_ms: int, state: str) -> bool:
        """Queue one process sample through the same bounded writer."""

    def drain(self, timeout: float | None = None) -> bool:
        """Wait for all accepted writes to finish."""

    def close(self) -> Mapping[str, Any]:
        """Drain and close the owned writer connection."""


@runtime_checkable
class TelemetrySampler(Protocol):
    """Fixed-cadence sampler lifecycle used by the runtime/container."""

    def start(self) -> Any:
        """Start and return the supervised sampler task."""

    async def shutdown(self) -> None:
        """Stop intake and await a started task."""

    async def wait_closed(self) -> None:
        """Await completion when a sampler was started."""


__all__ = [
    "AlertObservation",
    "AlertProfileState",
    "AlertSink",
    "StatusSnapshotProvider",
    "TelemetryCollector",
    "TelemetryDatabaseWriter",
    "TelemetrySampler",
]
