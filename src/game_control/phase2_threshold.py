"""Read-only Phase 2 transport decision harness."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import quantiles
from typing import Iterable, Mapping, Any

CONTRACT_VERSION = "phase2.1.v1"
MAX_EVENT_LOOP_VALUES = 16_384


@dataclass(frozen=True)
class FrozenThresholds:
    status_ui_p95_ms: float = 1_000.0
    slotd_cpu_p95_percent: float = 1.0
    web_cpu_p95_percent: float = 1.0
    event_loop_stall_p99_ms: float = 10.0
    reconnect_rate: float = 0.05
    bytes_per_subscriber: float = 65_536.0
    minimum_active_intervals: int = 3


@dataclass(frozen=True)
class ThresholdSamples:
    status_ui_latency_ms: tuple[float, ...] = ()
    slotd_cpu_percent: tuple[float, ...] = ()
    web_cpu_percent: tuple[float, ...] = ()
    event_loop_stall_ms: tuple[float, ...] = ()
    reconnects: int | None = None
    connection_attempts: int | None = None
    subscriber_bytes: int | None = None
    subscribers: int | None = None
    active_intervals: int = 0
    inactive_intervals: int = 0
    hidden_tab_requests: int | None = None


def _finite(values: Iterable[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(value != value or value in (float("inf"), float("-inf")) for value in result):
        raise ValueError("samples must be finite")
    return result


def _percentile(values: tuple[float, ...], percentile: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return float(quantiles(values, n=100, method="inclusive")[int(percentile * 100) - 1])


def evaluate_thresholds(
    samples: ThresholdSamples,
    thresholds: FrozenThresholds = FrozenThresholds(),
) -> dict[str, Any]:
    """Return a fail-closed decision; inactive intervals are not coverage."""
    if thresholds.minimum_active_intervals < 1:
        raise ValueError("minimum active intervals must be positive")
    if any(value != value or value in (float("inf"), float("-inf"))
           for value in (thresholds.status_ui_p95_ms, thresholds.slotd_cpu_p95_percent,
                         thresholds.web_cpu_p95_percent, thresholds.event_loop_stall_p99_ms,
                         thresholds.reconnect_rate, thresholds.bytes_per_subscriber)):
        raise ValueError("thresholds must be finite")
    if any(value < 0 for value in (thresholds.status_ui_p95_ms,
                                   thresholds.slotd_cpu_p95_percent,
                                   thresholds.web_cpu_p95_percent,
                                   thresholds.event_loop_stall_p99_ms,
                                   thresholds.reconnect_rate,
                                   thresholds.bytes_per_subscriber)):
        raise ValueError("thresholds must be non-negative")
    if samples.active_intervals < 0 or samples.inactive_intervals < 0:
        raise ValueError("interval counts must be non-negative")
    if samples.hidden_tab_requests is not None and samples.hidden_tab_requests < 0:
        raise ValueError("hidden-tab request count must be non-negative")
    values = {
        "status_ui_p95_ms": _percentile(_finite(samples.status_ui_latency_ms), 0.95)
        if len(samples.status_ui_latency_ms) >= 3 else None,
        "slotd_cpu_p95_percent": _percentile(_finite(samples.slotd_cpu_percent), 0.95)
        if len(samples.slotd_cpu_percent) >= 3 else None,
        "web_cpu_p95_percent": _percentile(_finite(samples.web_cpu_percent), 0.95)
        if len(samples.web_cpu_percent) >= 3 else None,
        "event_loop_stall_p99_ms": _percentile(_finite(samples.event_loop_stall_ms), 0.99)
        if len(samples.event_loop_stall_ms) >= 3 else None,
    }
    if samples.reconnects is not None and samples.connection_attempts is not None:
        if samples.reconnects < 0 or samples.connection_attempts <= 0:
            raise ValueError("reconnect counts must be non-negative and attempts positive")
        values["reconnect_rate"] = samples.reconnects / samples.connection_attempts
    else:
        values["reconnect_rate"] = None
    if samples.subscriber_bytes is not None and samples.subscribers is not None:
        if samples.subscriber_bytes < 0 or samples.subscribers <= 0:
            raise ValueError("subscriber bytes must be non-negative and subscribers positive")
        values["bytes_per_subscriber"] = samples.subscriber_bytes / samples.subscribers
    else:
        values["bytes_per_subscriber"] = None

    limits = {
        "status_ui_p95_ms": thresholds.status_ui_p95_ms,
        "slotd_cpu_p95_percent": thresholds.slotd_cpu_p95_percent,
        "web_cpu_p95_percent": thresholds.web_cpu_p95_percent,
        "event_loop_stall_p99_ms": thresholds.event_loop_stall_p99_ms,
        "reconnect_rate": thresholds.reconnect_rate,
        "bytes_per_subscriber": thresholds.bytes_per_subscriber,
        "hidden_tab_requests": 0,
    }
    values["hidden_tab_requests"] = samples.hidden_tab_requests
    checks = {
        name: {"value": values[name], "threshold": limit,
               "covered": values[name] is not None,
               "pass": values[name] is not None and values[name] <= limit}
        for name, limit in limits.items()
    }
    if samples.active_intervals < thresholds.minimum_active_intervals:
        checks["active_intervals"] = {"value": samples.active_intervals,
                                      "threshold": thresholds.minimum_active_intervals,
                                      "covered": False, "pass": False}
    missing = [name for name, check in checks.items() if not check["covered"]]
    failed = [name for name, check in checks.items() if check["covered"] and not check["pass"]]
    decision = "INCONCLUSIVE" if missing else ("JUSTIFY_PUSH" if failed else "SKIP_PUSH")
    return {
        "schemaVersion": CONTRACT_VERSION,
        "decision": decision,
        "thresholds": asdict(thresholds),
        "checks": checks,
        "missing": missing,
        "failed": failed,
        "coverage": {"activeIntervals": samples.active_intervals,
                     "inactiveIntervals": samples.inactive_intervals,
                     "hiddenTabRequests": samples.hidden_tab_requests},
        "readOnly": True,
    }


def samples_from_mapping(data: Mapping[str, Any]) -> ThresholdSamples:
    """Parse a bounded JSON fixture; reject unknown shape instead of guessing."""
    collector = data.get("collector")
    if isinstance(collector, Mapping) and collector.get("eventLoopSequenceValid") is False:
        raise ValueError("event-loop sequence contract is invalid")
    def numbers(name: str) -> tuple[float, ...]:
        value = data.get(name, [])
        limit = MAX_EVENT_LOOP_VALUES if name == "event_loop_stall_ms" else 10_000
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError(f"{name} must be a list of at most {limit} values")
        return _finite(value)

    kwargs: dict[str, Any] = {
        "status_ui_latency_ms": numbers("status_ui_latency_ms"),
        "slotd_cpu_percent": numbers("slotd_cpu_percent"),
        "web_cpu_percent": numbers("web_cpu_percent"),
        "event_loop_stall_ms": numbers("event_loop_stall_ms"),
    }
    for name in ("reconnects", "connection_attempts", "subscriber_bytes", "subscribers",
                 "active_intervals", "inactive_intervals", "hidden_tab_requests"):
        value = data.get(name)
        if value is None and name in {"active_intervals", "inactive_intervals"}:
            value = 0
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"{name} must be an integer or null")
        kwargs[name] = value
    return ThresholdSamples(**kwargs)
