"""Bounded disposable memory-pressure/OOM drill for Phase 4.2.

This module never targets a game unit, profile, or production cgroup.  It
launches only a fixed stdlib allocator under a uniquely named transient
systemd service in the disposable ``horizon-memory-drill.slice`` boundary.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "horizon-memory-drill.v1"
FIXTURE_PROFILE = "disposable-memory-drill"
FIXTURE_SLICE = "horizon-memory-drill.slice"
ALLOCATOR = "/usr/bin/python3"
MAX_DURATION_SECONDS = 15
STARTUP_MARGIN_SECONDS = 5
RUNTIME_MAX_SECONDS = MAX_DURATION_SECONDS + STARTUP_MARGIN_SECONDS
TIMEOUT_RUNTIME_MAX_SECONDS = 19
STARTUP_OBSERVATION_SECONDS = 1.0
MAX_MEMORY_MIB = 80
ALLOCATOR_HEADROOM_MIB = 24
MEMORY_HIGH = "64M"
MEMORY_MAX = "96M"
MEMORY_SWAP_MAX = "0"
OOM_POLICY = "kill"
ALLOCATOR_STARTUP_SECONDS = 0.25
PRESETS: dict[str, dict[str, Any]] = {
    "success": {
        "memory_mib": 48,
        "headroom_mib": 0,
        "memory_high": MEMORY_HIGH,
        "memory_max": MEMORY_MAX,
        "mode": "allocate-and-hold",
    },
    "oom": {
        "memory_mib": 80,
        "headroom_mib": ALLOCATOR_HEADROOM_MIB,
        # Keep the hard ceiling deterministic.  MemoryHigh=MemoryMax avoids
        # reclaim/throttle stalls before the allocator reaches the ceiling.
        "memory_high": MEMORY_MAX,
        "memory_max": MEMORY_MAX,
        "mode": "allocate-and-hold",
    },
    "timeout": {
        "memory_mib": 48,
        "headroom_mib": 0,
        "effective_mib": 0,
        "memory_high": MEMORY_MAX,
        "memory_max": MEMORY_MAX,
        "mode": "hold-no-pressure",
    },
}
DISPOSABLE_UNIT_RE = re.compile(
    r"horizon-memory-drill-[a-z0-9]+(?:-[a-z0-9]+)*\.service\Z"
)
DISPOSABLE_CGROUP_RE = re.compile(
    r"(?:/horizon-memory-drill\.slice/"
    r"|/horizon\.slice/horizon-memory\.slice/horizon-memory-drill\.slice/)"
    r"horizon-memory-drill-[a-z0-9]+(?:-[a-z0-9]+)*\.service\Z"
)
PRODUCTION_MARKERS = (
    "minecraft",
    "sunlit",
    "terraria",
    "game-slotd",
    "game-control-web",
    "games.slice",
    "maintenance.slice",
)

ALLOCATOR_SCRIPT = r"""
import sys
import time

duration = float(sys.argv[1])
memory_mib = int(sys.argv[2])
headroom_mib = int(sys.argv[3])
startup_seconds = float(sys.argv[4])
mode = sys.argv[5]
time.sleep(startup_seconds)
if mode == "allocate-and-hold":
    chunks = []
    while len(chunks) < memory_mib + headroom_mib:
        block = bytearray(1024 * 1024)
        for index in range(0, len(block), 4096):
            block[index] = 1
        chunks.append(block)
elif mode == "hold-no-pressure":
    # Timeout is intentionally independent of memory pressure.  systemd's
    # RuntimeMaxSec is the only expected termination mechanism.
    time.sleep(duration)
else:
    raise SystemExit("invalid allocator mode")
time.sleep(duration)
""".strip()


class MemoryDrillError(ValueError):
    """Raised when the disposable drill contract is violated."""


def validate_target(
    profile_id: str = FIXTURE_PROFILE,
    unit: str | None = None,
    cgroup: str | None = None,
) -> None:
    """Reject every target except the fixed disposable fixture boundary."""
    values = (profile_id, unit or "", cgroup or "")
    if any(any(marker in value.casefold() for marker in PRODUCTION_MARKERS) for value in values):
        raise MemoryDrillError("production game/profile/cgroup target is forbidden")
    if profile_id != FIXTURE_PROFILE:
        raise MemoryDrillError("memory drill profile is fixed")
    if unit is not None and (
        len(unit) > 128 or not DISPOSABLE_UNIT_RE.fullmatch(unit)
    ):
        raise MemoryDrillError("memory drill unit is fixed")
    if cgroup is not None and not DISPOSABLE_CGROUP_RE.fullmatch(cgroup):
        raise MemoryDrillError("memory drill cgroup is fixed")


def _bounded_duration(value: float) -> float:
    try:
        bounded = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoryDrillError("duration is not finite") from exc
    if not math.isfinite(bounded) or bounded <= 0 or bounded > MAX_DURATION_SECONDS:
        raise MemoryDrillError("duration exceeds the disposable drill bound")
    return bounded


def _bounded_memory(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryDrillError("memory request must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MemoryDrillError("memory request is not finite") from exc
    if not math.isfinite(numeric) or numeric <= 0 or numeric > MAX_MEMORY_MIB:
        raise MemoryDrillError("memory request exceeds the disposable drill bound")
    return value


def _preset(expect: str, memory_mib: int) -> dict[str, Any]:
    if expect not in PRESETS:
        raise MemoryDrillError("invalid expected drill result")
    selected = PRESETS[expect]
    if memory_mib != selected["memory_mib"]:
        raise MemoryDrillError("memory request does not match fixed pressure preset")
    return selected


def _runtime_max_seconds(expect: str) -> int:
    return TIMEOUT_RUNTIME_MAX_SECONDS if expect == "timeout" else RUNTIME_MAX_SECONDS


def build_argv(
    duration: float, memory_mib: int, unit: str, expect: str = "success"
) -> list[str]:
    duration = _bounded_duration(duration)
    memory_mib = _bounded_memory(memory_mib)
    selected = _preset(expect, memory_mib)
    validate_target(unit=unit)
    runtime_max_seconds = _runtime_max_seconds(expect)
    allocator_duration = (
        runtime_max_seconds + 10
        if expect == "timeout"
        else duration
    )
    return [
        "/usr/bin/systemd-run",
        "--no-block",
        "--quiet",
        "--service-type=exec",
        "--remain-after-exit",
        f"--unit={unit}",
        f"--slice={FIXTURE_SLICE}",
        f"--property=MemoryHigh={selected['memory_high']}",
        f"--property=MemoryMax={selected['memory_max']}",
        f"--property=MemorySwapMax={MEMORY_SWAP_MAX}",
        f"--property=OOMPolicy={OOM_POLICY}",
        f"--property=RuntimeMaxSec={runtime_max_seconds}s",
        "--property=TimeoutStopSec=5s",
        "--",
        ALLOCATOR,
        "-c",
        ALLOCATOR_SCRIPT,
        str(allocator_duration),
        str(memory_mib),
        str(selected["headroom_mib"]),
        str(ALLOCATOR_STARTUP_SECONDS),
        str(selected["mode"]),
    ]


def _show_argv(unit: str) -> list[str]:
    validate_target(unit=unit)
    return [
        "/usr/bin/systemctl",
        "show",
        "--no-pager",
        "--property=Slice,ControlGroup,MemoryHigh,MemoryMax,MemorySwapMax,OOMPolicy,ActiveState,SubState,Result,OOMKilled,ExecMainStatus,LoadState",
        unit,
    ]


def _parse_show(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {
            "Slice", "ControlGroup", "MemoryHigh", "MemoryMax", "MemorySwapMax",
            "OOMPolicy", "ActiveState", "SubState", "Result", "OOMKilled", "ExecMainStatus", "LoadState",
        }:
            values[key] = value
    return values


def _limits_match(values: Mapping[str, str], expect: str = "success") -> bool:
    selected = _preset(expect, PRESETS[expect]["memory_mib"])
    return (
        values.get("Slice") == FIXTURE_SLICE
        and DISPOSABLE_CGROUP_RE.fullmatch(values.get("ControlGroup", "")) is not None
        and values.get("MemoryHigh") in {selected["memory_high"], str(_memory_bytes(selected["memory_high"]))}
        and values.get("MemoryMax") in {selected["memory_max"], str(_memory_bytes(selected["memory_max"]))}
        and values.get("MemorySwapMax") in {MEMORY_SWAP_MAX, "0"}
        and values.get("OOMPolicy") == OOM_POLICY
    )


def _memory_bytes(value: str) -> int:
    return int(value[:-1]) * 1024 * 1024


def _interpret(values: Mapping[str, str], returncode: int, timed_out: bool) -> str:
    if values.get("OOMKilled") == "yes" or values.get("Result") == "oom-kill" or returncode in {137, 247}:
        return "oom"
    if timed_out:
        return "timeout"
    if values.get("Result") in {"timeout", "watchdog"}:
        return "timeout"
    if returncode == 0:
        return "success"
    return "failed"


def _workload_status(values: Mapping[str, str], launcher_status: int) -> int:
    raw = values.get("ExecMainStatus", "")
    try:
        return int(raw) if raw else launcher_status
    except (TypeError, ValueError):
        return launcher_status


def _poll_loaded(
    runner: Callable[..., Any],
    unit: str,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    deadline: float,
    *,
    startup_deadline: float | None = None,
    expect: str = "success",
    return_live: bool = False,
) -> dict[str, str] | tuple[dict[str, str], dict[str, str]]:
    """Retain the first valid live identity separately from terminal state.

    A transient unit commonly loses its ControlGroup after it exits.  The
    terminal observation is still authoritative for outcome, but it must not
    overwrite the live cgroup/limit evidence captured during startup.
    """
    values: dict[str, str] = {}
    live_values: dict[str, str] = {}
    loaded = False
    while True:
        now = clock()
        # Equality is already outside the polling window.  Using a strict
        # greater-than here can spin forever with a deterministic/frozen
        # clock that remains exactly at the deadline.
        if now >= deadline:
            break
        observed = runner(_show_argv(unit), check=False, capture_output=True, text=True)
        observed_values = _parse_show(getattr(observed, "stdout", ""))
        values = observed_values
        if values.get("LoadState") == "loaded":
            loaded = True
        if (
            not live_values
            and loaded
            and now <= (startup_deadline if startup_deadline is not None else deadline)
            and values.get("ActiveState") == "active"
            and values.get("SubState") not in {"exited", "dead"}
            and _limits_match(values, expect)
        ):
            live_values = dict(values)
        if loaded and (values.get("ActiveState") in {"inactive", "failed"} or values.get("SubState") in {"exited", "dead"}):
            return (values, live_values) if return_live else values
        sleeper(0.05)
    return (values, live_values) if return_live else values


def _cleanup(runner: Callable[..., Any], unit: str) -> bool:
    stop = runner(["/usr/bin/systemctl", "stop", unit], check=False, capture_output=True, text=True)
    reset = runner(["/usr/bin/systemctl", "reset-failed", unit], check=False, capture_output=True, text=True)
    unload = runner(["/usr/bin/systemctl", "daemon-reload"], check=False, capture_output=True, text=True)
    post = runner(_show_argv(unit), check=False, capture_output=True, text=True)
    values = _parse_show(getattr(post, "stdout", ""))
    gone = (
        values.get("LoadState") == "not-found"
        and not values.get("ControlGroup")
        and values.get("ActiveState") in {"", "inactive"}
        and values.get("SubState") in {"", "dead", "exited"}
    )
    # reset-failed returns EX_NOTFOUND (5) when stop already unloaded the
    # transient unit.  The final identity check is authoritative in that
    # narrow case; transport or daemon-reload errors remain failures.
    reset_not_loaded = getattr(reset, "stderr", "").rstrip("\n") == (
        f"Failed to reset failed state of unit {unit}: "
        f"Unit {unit} not loaded."
    )
    reset_ok = reset.returncode == 0 or (reset.returncode == 5 and gone) or (
        reset.returncode == 1 and gone and reset_not_loaded
    )
    # systemctl show may return EX_NOTFOUND (4) for an unloaded transient
    # while still returning the canonical not-found state above.  Accept that
    # narrow terminal form; any loaded/cgroup-bearing or unexpected result is
    # still fail-closed.
    post_ok = post.returncode == 0 or (post.returncode == 4 and gone)
    return stop.returncode == 0 and reset_ok and unload.returncode == 0 and post_ok and gone


def run_drill(
    *,
    duration: float = 3.0,
    memory_mib: int = 48,
    expect: str = "oom",
    runner: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the fixed fixture and return identity-free evidence."""
    if expect not in {"success", "oom", "timeout"}:
        raise MemoryDrillError("invalid expected drill result")
    duration = _bounded_duration(duration)
    memory_mib = _bounded_memory(memory_mib)
    _preset(expect, memory_mib)
    unit = f"horizon-memory-drill-{uuid.uuid4().hex[:12]}.service"
    validate_target(profile_id=FIXTURE_PROFILE, unit=unit)
    selected = _preset(expect, memory_mib)
    argv = build_argv(duration, memory_mib, unit, expect)
    runtime_max_seconds = _runtime_max_seconds(expect)
    started = clock()
    try:
        launch = runner(argv, check=False, capture_output=True, text=True, timeout=runtime_max_seconds + 5)
    except subprocess.TimeoutExpired:
        cleanup = _cleanup(runner, unit)
        return {
            "schema": SCHEMA,
            "profile": FIXTURE_PROFILE,
            "slice": FIXTURE_SLICE,
            "limits": {"MemoryHigh": selected["memory_high"], "MemoryMax": selected["memory_max"], "MemorySwapMax": MEMORY_SWAP_MAX, "OOMPolicy": OOM_POLICY},
            "preset": expect,
            "mode": selected["mode"],
            "requested_memory_mib": memory_mib,
            "effective_allocation_mib": selected.get("effective_mib", memory_mib + selected["headroom_mib"]),
            "expected_effective_allocation_mib": selected.get("effective_mib", memory_mib + selected["headroom_mib"]),
            "observed_limits_match": False,
            "result": "timeout",
            "expected": expect,
            "transport_timeout": True,
            # A launcher transport timeout provides no verified systemd unit
            # result or live limits, so it can never satisfy acceptance.
            "ok": False,
            "cleanup_verified": cleanup,
            "game_start_attempted": False,
            "identity_free": True,
            "duration_seconds": round(max(0.0, clock() - started), 3),
        }
    poll_deadline = started + runtime_max_seconds + 5
    values, live_values = _poll_loaded(
        runner, unit, clock, sleeper, poll_deadline,
        startup_deadline=started + STARTUP_OBSERVATION_SECONDS,
        expect=expect,
        return_live=True,
    )
    timed_out = values.get("ActiveState") == "active" and clock() >= poll_deadline
    # systemd-run's return code is launcher/transport status; the retained
    # unit's result fields are authoritative for the workload outcome.
    workload_status = _workload_status(values, launch.returncode)
    result = _interpret(values, workload_status, timed_out)
    if values.get("LoadState") != "loaded" and not timed_out:
        result = "failed"
    cleanup = _cleanup(runner, unit)
    return {
        "schema": SCHEMA,
        "profile": FIXTURE_PROFILE,
        "slice": FIXTURE_SLICE,
        "limits": {"MemoryHigh": selected["memory_high"], "MemoryMax": selected["memory_max"], "MemorySwapMax": MEMORY_SWAP_MAX, "OOMPolicy": OOM_POLICY},
        "preset": expect,
        "mode": selected["mode"],
        "requested_memory_mib": memory_mib,
        "effective_allocation_mib": selected.get("effective_mib", memory_mib + selected["headroom_mib"]),
        "expected_effective_allocation_mib": selected.get("effective_mib", memory_mib + selected["headroom_mib"]),
        "observed_limits_match": bool(live_values) and _limits_match(live_values, expect),
        "result": result,
        "expected": expect,
        "ok": result == expect and bool(live_values) and _limits_match(live_values, expect) and cleanup,
        "cleanup_verified": cleanup,
        "game_start_attempted": False,
        "identity_free": True,
        "duration_seconds": round(max(0.0, clock() - started), 3),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the bounded disposable Horizon memory drill")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--memory-mib", type=int, default=48)
    parser.add_argument("--expect", choices=("success", "oom", "timeout"), default="success")
    args = parser.parse_args(argv)
    try:
        evidence = run_drill(duration=args.duration, memory_mib=args.memory_mib, expect=args.expect)
    except (MemoryDrillError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(exc), "identity_free": True}, sort_keys=True))
        return 2
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
