"""Bounded, authenticated, read-only observations for Phase 2.1."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from pathlib import Path

MAX_BODY = 256 * 1024
MAX_EVENT_LOOP_WINDOW = 1_024
MAX_EVENT_LOOP_VALUES = 16_384
READ_ONLY_PATHS = frozenset({"/api/v1/status", "/api/v1/perf", "/api/v1/stream"})


@dataclass(frozen=True)
class CollectorConfig:
    base_url: str
    token: str
    duration_seconds: float = 30.0
    interval_seconds: float = 3.0
    actor: str = "threshold-harness"
    slotd_pid: int | None = None
    web_pid: int | None = None
    slotd_cgroup: str | None = None
    web_cgroup: str | None = None
    include_sample_timing: bool = False


def _validated_logical_cpu_count(psutil_module: Any) -> int:
    """Return the host logical CPU count used for process CPU normalization."""
    count = psutil_module.cpu_count(logical=True)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("logical CPU count must be a positive integer")
    return count


def normalize_process_cpu_percent(raw_percent: float, logical_cpu_count: int) -> float:
    """Convert psutil's process percent to a fraction of host capacity once."""
    count = logical_cpu_count
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("logical CPU count must be a positive integer")
    value = float(raw_percent)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise ValueError("process CPU percent must be finite and non-negative")
    return value / count


def validate_process_identity(pid: int, expected_cgroup: str, *, proc_root: Path = Path("/proc")) -> tuple[int, str]:
    """Pin PID reuse by start time and require the expected cgroup membership."""
    if not isinstance(pid, int) or pid <= 0 or not expected_cgroup or len(expected_cgroup) > 256:
        raise ValueError("invalid process identity")
    stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
    cgroup = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    if expected_cgroup not in cgroup:
        raise ValueError("process is outside expected cgroup")
    fields = stat.rsplit(")", 1)[-1].split()
    if len(fields) < 20:
        raise ValueError("malformed process stat")
    return int(fields[19]), cgroup


def _validate_base_url(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("base URL must be an HTTP(S) URL with a host and no credentials")
    if len(base_url) > 2048 or parsed.port and not (1 <= parsed.port <= 65535):
        raise ValueError("base URL is invalid or oversized")
    return parsed.scheme, parsed.netloc


def _get_json(config: CollectorConfig, path: str, opener: Callable[..., Any]) -> tuple[float, dict[str, Any]]:
    if path not in READ_ONLY_PATHS:
        raise ValueError("collector path is not allowlisted")
    scheme, host = _validate_base_url(config.base_url)
    started = time.monotonic()
    request = Request(config.base_url.rstrip("/") + path, method="GET", headers={
        "X-Game-Control-Proxy": config.token,
        "X-authentik-username": config.actor,
        "Accept": "application/json",
    })
    with opener(request, timeout=5) as response:
        final = urlsplit(response.geturl())
        if final.scheme != scheme or final.netloc != host:
            raise ValueError("redirect left the configured collector origin")
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            raise ValueError("JSON endpoint returned an unexpected content type")
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("response exceeds bounded collector body")
    return (time.monotonic() - started) * 1000.0, json.loads(body)


def _sse_bytes(config: CollectorConfig, opener: Callable[..., Any]) -> tuple[int, bool]:
    scheme, host = _validate_base_url(config.base_url)
    request = Request(config.base_url.rstrip("/") + "/api/v1/stream", method="GET", headers={
        "X-Game-Control-Proxy": config.token,
        "X-authentik-username": config.actor,
        "Accept": "text/event-stream",
    })
    try:
        with opener(request, timeout=1) as response:
            final = urlsplit(response.geturl())
            if final.scheme != scheme or final.netloc != host:
                raise ValueError("redirect left the configured collector origin")
            if "text/event-stream" not in response.headers.get("Content-Type", ""):
                raise ValueError("stream returned an unexpected content type")
            data = response.read(65_537)
        if len(data) > 65_536:
            raise ValueError("stream exceeds bounded collector payload")
        return len(data), True
    except ValueError:
        raise
    except Exception:
        return 0, False


def _collect_sequence_values(
    values: Any, sequence: Any, cursor: int | None, *, label: str,
) -> tuple[list[float], int | None]:
    """Return only unseen sequence values; reject malformed/overwritten windows."""
    if (not isinstance(values, list) or len(values) > MAX_EVENT_LOOP_WINDOW
            or not isinstance(sequence, dict)
            or not isinstance(sequence.get("start"), int)
            or not isinstance(sequence.get("end"), int)
            or isinstance(sequence.get("start"), bool)
            or isinstance(sequence.get("end"), bool)
            or sequence["start"] < 0
            or sequence["end"] < sequence["start"]
            or sequence["end"] - sequence["start"] != len(values)):
        raise ValueError(f"malformed {label} sequence window")
    start, end = sequence["start"], sequence["end"]
    if cursor is None:
        return [], end
    if start > cursor or end < cursor:
        raise ValueError(f"{label} sequence gap or regression")
    offset = cursor - start
    return [float(value) for value in values[offset:]], end


def _collect_event_loop_values(loop: Any, sequence: Any, cursor: int | None):
    return _collect_sequence_values(loop, sequence, cursor, label="event-loop")


def collect(config: CollectorConfig, *, opener: Callable[..., Any] = urlopen) -> dict[str, Any]:
    """Collect only GET observations; no lifecycle or mutation route is callable."""
    if config.duration_seconds <= 0 or config.duration_seconds > 3600:
        raise ValueError("duration must be between 0 and 3600 seconds")
    if config.interval_seconds <= 0 or config.interval_seconds > 60:
        raise ValueError("interval must be between 0 and 60 seconds")
    _validate_base_url(config.base_url)
    collection_started = time.monotonic()
    deadline = collection_started + config.duration_seconds
    latencies: list[float] = []
    event_loop: list[float] = []
    event_loop_cursor: int | None = None
    slotd_cpu: list[float] = []
    web_cpu: list[float] = []
    sse_bytes = 0
    sse_attempts = 0
    samples = 0
    sample_monotonic_offsets_ms: list[float] = []
    errors = 0
    event_loop_contract_error = False
    maintenance_contract_error = False
    maintenance_values: list[float] = []
    maintenance_observations: list[dict[str, float | int]] = []
    maintenance_cursor: int | None = None
    processes: dict[str, Any] = {}
    process_baselines: dict[str, tuple[int, str]] = {}
    identity_requested = any(isinstance(pid, int) and pid > 0
                             for pid in (config.slotd_pid, config.web_pid))
    process_identity_valid = not identity_requested
    process_identity_error = False
    cpu_normalization: dict[str, Any] = {
        "applied": False, "logicalCpuCount": None, "source": None,
    }
    try:
        import psutil
        logical_cpu_count = None
        for label, pid, cgroup in (("slotd", config.slotd_pid, config.slotd_cgroup),
                                   ("web", config.web_pid, config.web_cgroup)):
            if pid is not None and pid > 0:
                if not cgroup:
                    raise ValueError(f"{label} cgroup identity is required")
                if logical_cpu_count is None:
                    logical_cpu_count = _validated_logical_cpu_count(psutil)
                process_baselines[label] = validate_process_identity(pid, cgroup)
                processes[label] = psutil.Process(pid)
                processes[label].cpu_percent(None)
            process_identity_valid = bool(process_baselines) and len(process_baselines) == sum(
                1 for pid in (config.slotd_pid, config.web_pid) if isinstance(pid, int) and pid > 0)
        if logical_cpu_count is not None:
            cpu_normalization = {
                "applied": True, "logicalCpuCount": logical_cpu_count,
                "source": "psutil_process_cpu_percent",
                "formula": "raw_percent / logical_cpu_count",
            }
    except ImportError:
        processes = {}
        process_identity_valid = False if identity_requested else process_identity_valid
    except (OSError, psutil.Error):
        processes = {}
        process_identity_valid = False if identity_requested else process_identity_valid
        process_identity_error = identity_requested
    while time.monotonic() < deadline and samples < 1200:
        try:
            sample_started = time.monotonic()
            latency, status = _get_json(config, "/api/v1/status", opener)
            _, perf = _get_json(config, "/api/v1/perf", opener)
            pending_event_loop: list[float] = []
            pending_maintenance: list[float] = []
            pending_observations: list[dict[str, float | int]] = []
            pending_slotd_cpu: list[float] = []
            pending_web_cpu: list[float] = []
            pending_event_cursor = event_loop_cursor
            pending_maintenance_cursor = maintenance_cursor
            # Cycle duration is not event-loop lag. Only a dedicated lag ring
            # can populate this field; absent support is intentionally missing.
            loop = perf.get("slotd", {}).get("event_loop_lag_ms")
            sequence = perf.get("slotd", {}).get("event_loop_lag_sequence")
            if loop is not None or sequence is not None:
                unseen, pending_event_cursor = _collect_sequence_values(
                    loop, sequence, pending_event_cursor, label="event-loop")
                pending_event_loop.extend(unseen)
            maintenance = perf.get("slotd", {}).get("maintenance_ms")
            maintenance_sequence = perf.get("slotd", {}).get("maintenance_sequence")
            if maintenance is not None or maintenance_sequence is not None:
                prior_cursor = pending_maintenance_cursor
                unseen, pending_maintenance_cursor = _collect_sequence_values(
                    maintenance, maintenance_sequence, pending_maintenance_cursor, label="maintenance")
                pending_maintenance.extend(unseen)
                if config.include_sample_timing and unseen:
                    base = maintenance_sequence["start"] if prior_cursor is None else prior_cursor
                    offset_ms = (sample_started - collection_started) * 1000.0
                    pending_observations.extend(
                        {"value": value, "sequence_end": base + index + 1,
                         "sample_offset_ms": offset_ms}
                        for index, value in enumerate(unseen)
                    )
            # The perf endpoint is authoritative for route timing; use its
            # p95 values when present and keep CPU coverage explicit otherwise.
            for value in perf.get("slotd", {}).get("cpu_percent", []):
                pending_slotd_cpu.append(float(value))
            for value in perf.get("web", {}).get("cpu_percent", []):
                pending_web_cpu.append(float(value))
            if process_identity_error:
                slotd_cpu.clear()
                web_cpu.clear()
            if processes:
                try:
                    for label, samples_for_process in (("slotd", slotd_cpu), ("web", web_cpu)):
                        if label not in processes:
                            continue
                        observed_start, observed_cgroup = validate_process_identity(
                            getattr(config, f"{label}_pid"), getattr(config, f"{label}_cgroup"))
                        baseline_start, baseline_cgroup = process_baselines[label]
                        if ((observed_start, observed_cgroup)
                                != (baseline_start, baseline_cgroup)):
                            raise ValueError(f"{label} process identity changed")
                        pending_samples = pending_slotd_cpu if label == "slotd" else pending_web_cpu
                        pending_samples.append(normalize_process_cpu_percent(
                            processes[label].cpu_percent(None), cpu_normalization["logicalCpuCount"]))
                except Exception:
                    # A vanished or reused PID invalidates the whole process
                    # CPU series; leave it uncovered rather than using stale
                    # samples to reach a threshold decision.
                    process_identity_valid = False
                    process_identity_error = True
                    processes.clear()
                    slotd_cpu.clear()
                    web_cpu.clear()
                    errors += 1
                    raise RuntimeError("process sample invalid")
            # One bounded subscription per run. A real browser/session
            # harness must provide sustained disconnect/reconnect evidence.
            if sse_attempts == 0:
                sse_attempts = 1
                received, _connected = _sse_bytes(config, opener)
                sse_bytes += received
            # Commit only after both sequence contracts, process identity, and
            # the bounded SSE attempt have completed for this sample.
            latencies.append(latency)
            event_loop.extend(pending_event_loop)
            event_loop_cursor = pending_event_cursor
            maintenance_values.extend(pending_maintenance)
            maintenance_cursor = pending_maintenance_cursor
            maintenance_observations.extend(pending_observations)
            slotd_cpu.extend(pending_slotd_cpu)
            web_cpu.extend(pending_web_cpu)
            if config.include_sample_timing:
                sample_monotonic_offsets_ms.append((sample_started - collection_started) * 1000.0)
            samples += 1
            del status
        except ValueError as exc:
            if "event-loop" in str(exc):
                event_loop_contract_error = True
                event_loop.clear()
            elif "maintenance" in str(exc):
                maintenance_contract_error = True
                maintenance_values.clear()
            errors += 1
        except Exception:
            errors += 1
        time.sleep(min(config.interval_seconds, max(0.0, deadline - time.monotonic())))
    result = {
        "status_ui_latency_ms": [],
        "status_rtt_proxy_ms": latencies,
        "slotd_cpu_percent": slotd_cpu,
        "web_cpu_percent": web_cpu,
        "event_loop_stall_ms": event_loop,
        "reconnects": None,
        "connection_attempts": None,
        "subscriber_bytes": sse_bytes,
        "subscribers": 1 if sse_attempts else None,
        "active_intervals": samples,
        "inactive_intervals": 0,
        "hidden_tab_requests": None,
        "collector": {"schemaVersion": "phase2.1.inputs.v1", "samples": samples,
                      "errors": errors, "readOnly": True, "maxBodyBytes": MAX_BODY,
                      "eventLoopSequence": True, "eventLoopSequenceValid": not event_loop_contract_error,
                      "maxEventLoopWindow": MAX_EVENT_LOOP_WINDOW,
                      "maxEventLoopValues": MAX_EVENT_LOOP_VALUES,
                      "cpuNormalization": cpu_normalization,
                      "processIdentity": {"requested": identity_requested,
                                          "valid": process_identity_valid,
                                          "error": process_identity_error}},
    }
    if config.include_sample_timing:
        result["maintenance_tick_ms"] = maintenance_values
        result["maintenance_observations"] = maintenance_observations
        result["sample_monotonic_offsets_ms"] = sample_monotonic_offsets_ms
        result["collector"].update({
            "maintenanceSequence": True,
            "maintenanceSequenceValid": not maintenance_contract_error,
            "sampleTiming": True,
        })
    return result
