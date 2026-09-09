#!/usr/bin/env python3
"""Summarize phase-labelled, normalized CPU observations.

This is deliberately a pure evidence transformation.  It does not collect
process data, identify a game/server/player, or alter acceptance thresholds.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "phase2.1.cpu-phases.v1"
PHASES = ("startup", "ready-empty", "gameplay", "stopped", "transition", "unknown")
PROCESSES = frozenset(("slotd", "web"))
MAX_OBSERVATIONS_PER_PROCESS = 1200
MAX_OBSERVATIONS = MAX_OBSERVATIONS_PER_PROCESS * len(PROCESSES) * 2
MAX_CPU = 100.0
MAX_SECONDS = 3600.0

_INPUT_KEYS = frozenset(("cpu_observations", "cpu_warmup_observations", "cpu_normalization",
                         "process_identity", "slotd_cpu_percent", "web_cpu_percent"))
_OBS_KEYS = frozenset(("process", "cpu_percent", "start_monotonic", "end_monotonic", "duration_seconds", "phase"))
_NORM_KEYS = frozenset(("applied", "logicalCpuCount", "source", "formula"))
_IDENTITY_KEYS = frozenset(("requested", "valid", "error"))
_CPU_SOURCE = "psutil_process_cpu_percent"
_CPU_FORMULA = "raw_percent / logical_cpu_count"


def _finite_number(value: Any, name: str, *, minimum: float = 0.0, maximum: float = MAX_SECONDS) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} is outside its bounded finite range")
    return value


def _normalization(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _NORM_KEYS:
        raise ValueError("cpu_normalization must exactly match its allowlist")
    if not isinstance(value["applied"], bool):
        raise ValueError("cpu normalization applied flag is invalid")
    count = value["logicalCpuCount"]
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 4096):
        raise ValueError("logical CPU count is invalid")
    if value["applied"] is not True or not isinstance(count, int) or count <= 0:
        raise ValueError("CPU normalization is not applied")
    if value["source"] != _CPU_SOURCE:
        raise ValueError("CPU source is invalid")
    if value["formula"] != _CPU_FORMULA:
        raise ValueError("CPU normalization formula is invalid")
    return {key: value[key] for key in ("applied", "logicalCpuCount", "source", "formula")}


def _observation(value: Any, *, warmup: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= _OBS_KEYS:
        raise ValueError("CPU observation contains an unapproved field")
    required = _OBS_KEYS - {"phase"}
    if not required <= set(value):
        raise ValueError("CPU observation is missing required timing fields")
    process = value["process"]
    if not isinstance(process, str) or process not in PROCESSES:
        raise ValueError("CPU observation process is invalid")
    cpu = _finite_number(value["cpu_percent"], "cpu_percent", maximum=MAX_CPU)
    start = _finite_number(value["start_monotonic"], "start_monotonic", maximum=1e12)
    end = _finite_number(value["end_monotonic"], "end_monotonic", maximum=1e12)
    duration = _finite_number(value["duration_seconds"], "duration_seconds", minimum=1e-9, maximum=MAX_SECONDS)
    if end < start or not math.isclose(end - start, duration, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("CPU observation timing is inconsistent")
    phase = value.get("phase", "unknown")
    if not isinstance(phase, str) or phase not in PHASES:
        raise ValueError("CPU observation phase is invalid")
    return {"process": process, "cpu_percent": cpu, "start_monotonic": start,
            "end_monotonic": end, "duration_seconds": duration, "phase": phase, "warmup": warmup}


def _stats(values: list[float], duration: float) -> dict[str, Any]:
    return {
        "count": len(values),
        "duration_seconds": duration,
        "median": statistics.median(values) if len(values) >= 3 else None,
        "p95": _percentile(values, 0.95) if len(values) >= 3 else None,
        "max": max(values) if values else None,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    # Frozen interpretation: floor((n - 1) * quantile), with no interpolation.
    return ordered[math.floor((len(ordered) - 1) * quantile)]


def summarize_cpu_phases(inputs: dict[str, Any]) -> dict[str, Any]:
    """Return bounded CPU summaries grouped by process and lifecycle phase.

    Warmup observations are validated for shape but never included.  Missing
    timestamps means this is a legacy dataset and is reported as unavailable.
    """
    if not isinstance(inputs, dict) or not set(inputs) <= _INPUT_KEYS:
        raise ValueError("CPU phase input contains an unapproved field")
    # These keys are the pre-phase collector shape. Keep the result explicit
    # and unavailable rather than treating an un-timestamped value as zero.
    if "slotd_cpu_percent" in inputs or "web_cpu_percent" in inputs:
        return {"schemaVersion": SCHEMA_VERSION, "status": "unavailable", "reason": "legacy missing timing"}
    observations = inputs.get("cpu_observations", [])
    warmups = inputs.get("cpu_warmup_observations", [])
    if not isinstance(observations, list) or not isinstance(warmups, list) or len(observations) + len(warmups) > MAX_OBSERVATIONS:
        raise ValueError("CPU observation list is oversized or malformed")
    if any(isinstance(item, dict) and not (_OBS_KEYS - {"phase"}) <= set(item) for item in observations):
        return {"schemaVersion": SCHEMA_VERSION, "status": "unavailable", "reason": "legacy missing timing"}
    parsed = [_observation(item, warmup=False) for item in observations]
    parsed_warmups = [_observation(item, warmup=True) for item in warmups]
    for process in PROCESSES:
        steady = [item for item in parsed if item["process"] == process]
        warm = [item for item in parsed_warmups if item["process"] == process]
        for series in (steady, warm):
            previous_end = None
            for item in series:
                if previous_end is not None and item["start_monotonic"] < previous_end:
                    raise ValueError("CPU observation intervals overlap or are unordered")
                previous_end = item["end_monotonic"]
        rows = sorted(steady + warm, key=lambda item: item["start_monotonic"])
        if sum(not item["warmup"] for item in rows) > MAX_OBSERVATIONS_PER_PROCESS or sum(item["warmup"] for item in rows) > MAX_OBSERVATIONS_PER_PROCESS:
            raise ValueError("CPU observations exceed per-process bound")
        previous_end = None
        for item in rows:
            if previous_end is not None and item["start_monotonic"] < previous_end:
                raise ValueError("CPU observation intervals overlap or are unordered")
            previous_end = item["end_monotonic"]
        intervals = [(item["start_monotonic"], item["end_monotonic"]) for item in rows]
        if len(intervals) != len(set(intervals)):
            raise ValueError("CPU observation intervals duplicate")
    if not parsed:
        return {"schemaVersion": SCHEMA_VERSION, "status": "unavailable", "reason": "no non-warmup CPU samples"}
    normalization = _normalization(inputs["cpu_normalization"]) if "cpu_normalization" in inputs else None
    if normalization is None:
        return {"schemaVersion": SCHEMA_VERSION, "status": "unavailable", "reason": "missing CPU normalization metadata"}
    identity = inputs.get("process_identity")
    if (not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS
            or any(type(identity[key]) is not bool for key in _IDENTITY_KEYS)
            or identity != {"requested": True, "valid": True, "error": False}):
        return {"schemaVersion": SCHEMA_VERSION, "status": "unavailable", "reason": "CPU process identity is untrusted",
                "normalization": normalization}
    process_summary: dict[str, dict[str, dict[str, Any]]] = {}
    for process in sorted(PROCESSES):
        process_summary[process] = {}
        for phase in PHASES:
            rows = [item for item in parsed if item["process"] == process and item["phase"] == phase]
            process_summary[process][phase] = _stats([row["cpu_percent"] for row in rows], sum(row["duration_seconds"] for row in rows))
    return {"schemaVersion": SCHEMA_VERSION, "status": "available", "normalization": normalization,
            "processes": {process: {**values, "all": _stats(
                [item["cpu_percent"] for item in parsed if item["process"] == process],
                sum(item["duration_seconds"] for item in parsed if item["process"] == process))}
                for process, values in process_summary.items()}}


report_cpu_phases = summarize_cpu_phases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=argparse.FileType("r"))
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()
    input_path = getattr(args.input, "name", None)
    output_path = Path(args.output)
    if input_path and output_path.resolve() == Path(input_path).resolve():
        raise SystemExit("input and output paths must differ")
    raw = args.input.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise SystemExit("input exceeds bounded size")
    result = summarize_cpu_phases(json.loads(raw))
    with output_path.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
