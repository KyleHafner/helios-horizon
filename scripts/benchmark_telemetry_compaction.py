#!/usr/bin/env python3
"""Isolated before/after benchmark for telemetry per-write compaction.

The baseline is loaded from ``git show HEAD:...`` and the candidate from the
working tree.  No production paths or databases are used.
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import subprocess
import tempfile
import time
from types import SimpleNamespace
from typing import Any


REPO = Path(__file__).resolve().parents[1]
MODULE_REL = Path("src/game_control/telemetry_db.py")
BASE_MS = 2_000_000_000_000
RETENTION_MS = 60 * 60 * 1000
WORKLOAD_WRITES = 1_200
EXPIRED_ROWS_PER_SERIES = 250
REPETITIONS = 3
PROFILE = "bench-node"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_module(temp_dir: Path) -> Path:
    target = temp_dir / "telemetry_db_baseline.py"
    source = subprocess.check_output(
        ["git", "-C", str(REPO), "show", f"HEAD:{MODULE_REL}"], text=True
    )
    target.write_text(source, encoding="utf-8")
    return target


def seed_database(module: Any, path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    module._create_v2_schema(connection)
    connection.execute("PRAGMA user_version = 2")
    series = [
        ("resource.bench-node.cpu_percent", "cpu_percent", "percent", "gauge"),
        ("resource.bench-node.rss_bytes", "rss_bytes", "bytes", "gauge"),
        ("resource.bench-node.disk_read_bps", "disk_read_bps", "bytes_per_second", "gauge"),
        ("resource.bench-node.disk_write_bps", "disk_write_bps", "bytes_per_second", "gauge"),
        ("resource.bench-node.players", "players", "players", "gauge"),
        ("resource.bench-node.tps", "tps", "tps", "gauge"),
        ("resource.bench-node.service_io_read_bytes_total", "service_io_read_bytes_total", "bytes", "counter"),
    ]
    connection.executemany(
        "INSERT INTO telemetry_series(series_id,profile_id,metric,unit,kind,labels_json) VALUES(?,?,?,?,?,'{}')",
        [(sid, PROFILE, metric, unit, kind) for sid, metric, unit, kind in series],
    )
    rows = []
    for series_index, (sid, _metric, _unit, _kind) in enumerate(series):
        for index in range(EXPIRED_ROWS_PER_SERIES):
            # Multiple old buckets make the compactor do representative work.
            ts = BASE_MS - RETENTION_MS - 2_000_000 + index * 1_000 + series_index
            if series_index == len(series) - 1:
                # Include reset-aware counter deltas and continuity gaps.
                state = "unavailable" if index % 83 == 0 else ("inactive" if index % 97 == 0 else "available")
                value = None if state != "available" else float((index % 80) * 1000)
                rows.append((sid, ts, value, state, int(state != "inactive")))
            else:
                rows.append((sid, ts, float(series_index * 10 + index % 17), "available", 1))
        # Retained rows must survive the write workload and final compaction.
        rows.append((sid, BASE_MS - 30_000 + series_index, float(series_index + 1), "available", 1))
    connection.executemany("INSERT INTO telemetry_samples VALUES(?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def canonical_snapshot(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    connection = sqlite3.connect(path)
    tables = (
        "telemetry_series", "telemetry_samples", "telemetry_rollups",
        "telemetry_state_rollups", "telemetry_counter_baselines",
        "telemetry_state_baselines",
    )
    result: dict[str, list[tuple[Any, ...]]] = {}
    for table in tables:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        # Normalize SQLite integer/float representation for a strict semantic comparison.
        normalized = []
        for row in rows:
            normalized.append(tuple(float(value) if isinstance(value, float) and math.isfinite(value) else value for value in row))
        result[table] = sorted(normalized, key=repr)
    connection.close()
    return result


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def run_case(module: Any, label: str, database: Path) -> dict[str, Any]:
    calls = 0
    original_compact = module._compact_connection
    original_time = module.time
    clock = [0.0]

    class ModuleTimeProxy:
        def monotonic(self) -> float:
            return clock[0]

        def __getattr__(self, name: str) -> Any:
            return getattr(original_time, name)

    def counted(connection, *, now_ms, raw_retention_ms):
        nonlocal calls
        calls += 1
        return original_compact(connection, now_ms=now_ms, raw_retention_ms=raw_retention_ms)

    module._compact_connection = counted
    module.time = ModuleTimeProxy()
    database.parent.mkdir(mode=0o700)
    seed_database(module, database)
    store = module.TelemetryDatabase.open(database, retention_ms=RETENTION_MS, queue_size=8)
    worker_connection = sqlite3.connect(database)
    worker_connection.execute("PRAGMA busy_timeout = 5000")
    worker_connection.execute("PRAGMA journal_mode = WAL")
    worker_connection.execute("PRAGMA synchronous = NORMAL")
    worker_connection.execute("PRAGMA foreign_keys = ON")
    worker_connection.set_authorizer(module._deny_attach)
    write_times: list[float] = []
    cpu_start = time.process_time_ns()
    wall_start = time.perf_counter_ns()
    try:
        for index in range(WORKLOAD_WRITES):
            ts_ms = BASE_MS + index * 1_000
            clock[0] = index * 1.0
            start = time.perf_counter_ns()
            if index % 5 == 0:
                sample = SimpleNamespace(
                    cpu_percent=20.0 + (index % 11),
                    rss_bytes=500_000_000 + index * 100,
                    disk_read_bps=1_000.0 + index,
                    disk_write_bps=500.0 + index / 2,
                )
                store._write_with(worker_connection, PROFILE, sample=sample, ts_ms=ts_ms, state="available")
            elif index % 5 == 1:
                store._write_generic_with(worker_connection, PROFILE, "players", index % 12,
                                           ts_ms=ts_ms, state="available", labels={})
            elif index % 5 == 2:
                store._write_generic_with(worker_connection, PROFILE, "tps", 19.5 + (index % 7) / 10,
                                           ts_ms=ts_ms, state="available", labels={})
            else:
                counter_state = "unavailable" if index % 101 == 0 else ("inactive" if index % 137 == 0 else "available")
                counter_value = None if counter_state != "available" else float((index % 61) * 1000)
                store._write_generic_with(worker_connection, PROFILE, "service_io_read_bytes_total", counter_value,
                                           ts_ms=ts_ms, state=counter_state, labels={})
            write_times.append((time.perf_counter_ns() - start) / 1_000_000)
        write_wall_ms = (time.perf_counter_ns() - wall_start) / 1_000_000
        write_cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
        write_compaction_calls = calls
        # Force an equivalent complete compaction for both versions. This is
        # intentionally outside write timing and catches deferred-work tricks.
        final_now = BASE_MS + WORKLOAD_WRITES * 1_000 + RETENTION_MS + 1
        force_start = time.perf_counter_ns()
        force_result = store.compact_hourly(now_ms=final_now)
        force_ms = (time.perf_counter_ns() - force_start) / 1_000_000
        total_wall_ms = (time.perf_counter_ns() - wall_start) / 1_000_000
        total_cpu_ms = (time.process_time_ns() - cpu_start) / 1_000_000
        force_compaction_calls = calls - write_compaction_calls
        store.connection.commit()
        snapshot = canonical_snapshot(database)
        return {
            "label": label,
            "workload_writes": WORKLOAD_WRITES,
            "write_wall_ms": write_wall_ms,
            "write_cpu_ms": write_cpu_ms,
            "total_wall_ms": total_wall_ms,
            "total_cpu_ms": total_cpu_ms,
            "write_wall_p95_ms": percentile(write_times, 0.95),
            "write_wall_mean_ms": statistics.fmean(write_times),
            "compaction_calls_during_writes": write_compaction_calls,
            "force_compaction_calls": force_compaction_calls,
            "force_compaction_wall_ms": force_ms,
            "force_result": force_result,
            "canonical_snapshot": snapshot,
        }
    finally:
        worker_connection.close()
        store.close()
        module._compact_connection = original_compact
        module.time = original_time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="compaction-results.json")
    parser.add_argument("--report", default="compaction-results.md")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="telemetry-bench-") as raw:
        temp_dir = Path(raw)
        baseline = load_module("telemetry_db_baseline", baseline_module(temp_dir))
        candidate_path = REPO / MODULE_REL
        candidate = load_module("telemetry_db_candidate", candidate_path)
        source_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        commit_sha = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()
        raw_runs = []
        for repetition in range(REPETITIONS):
            order = ((baseline, "baseline_head"), (candidate, "candidate_worktree")) if repetition % 2 == 0 else ((candidate, "candidate_worktree"), (baseline, "baseline_head"))
            for module, label in order:
                result = run_case(module, label, temp_dir / f"run-{repetition}-{label}" / "telemetry.sqlite")
                result["repetition"] = repetition + 1
                raw_runs.append(result)
    equivalent = all(
        next(r for r in raw_runs if r["label"] == "baseline_head" and r["repetition"] == n)["canonical_snapshot"]
        == next(r for r in raw_runs if r["label"] == "candidate_worktree" and r["repetition"] == n)["canonical_snapshot"]
        for n in range(1, REPETITIONS + 1)
    )
    results = []
    for label in ("baseline_head", "candidate_worktree"):
        runs = [r for r in raw_runs if r["label"] == label]
        numeric = ("write_wall_ms", "write_cpu_ms", "write_wall_p95_ms", "write_wall_mean_ms", "force_compaction_wall_ms", "total_wall_ms", "total_cpu_ms")
        aggregate = {key: statistics.fmean(r[key] for r in runs) for key in numeric}
        aggregate.update({"label": label, "repetitions": REPETITIONS,
                          "workload_writes": WORKLOAD_WRITES,
                          "compaction_calls_during_writes": [r["compaction_calls_during_writes"] for r in runs],
                          "force_compaction_calls": [r["force_compaction_calls"] for r in runs],
                          "force_result": runs[0]["force_result"]})
        aggregate["runs"] = [{key: value for key, value in run.items() if key != "canonical_snapshot"} for run in runs]
        results.append(aggregate)
    payload = {"schema": 1, "source": {"baseline_commit": commit_sha, "candidate_commit": commit_sha,
        "candidate_telemetry_db_sha256": source_sha}, "fixture": {"base_ms": BASE_MS, "retention_ms": RETENTION_MS,
        "expired_rows_per_series": EXPIRED_ROWS_PER_SERIES, "repetitions": REPETITIONS}, "results": results,
        "canonical_contents_equivalent_after_force_compaction": equivalent}
    Path(args.json).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    b, c = results
    report = f"""# Telemetry per-write compaction benchmark

Command: `python3 scripts/benchmark_telemetry_compaction.py`

The benchmark seeds an isolated SQLite database with {EXPIRED_ROWS_PER_SERIES} expired rows per series plus retained rows, counter resets/gaps, and inactive/unavailable states, then performs {WORKLOAD_WRITES} deterministic mixed resource/generic writes. Three paired repetitions alternate execution order. CPU and wall measurements cover both writes and the explicit equivalent final force-compaction; canonical table contents were equivalent: **{equivalent}**.

| case | write wall (ms) | write CPU (ms) | total wall (ms) | total CPU (ms) | write p95 (ms) | compaction calls during writes |
|---|---:|---:|---:|---:|---:|---:|
| baseline HEAD | {b['write_wall_ms']:.3f} | {b['write_cpu_ms']:.3f} | {b['total_wall_ms']:.3f} | {b['total_cpu_ms']:.3f} | {b['write_wall_p95_ms']:.6f} | {b['compaction_calls_during_writes']} |
| candidate worktree | {c['write_wall_ms']:.3f} | {c['write_cpu_ms']:.3f} | {c['total_wall_ms']:.3f} | {c['total_cpu_ms']:.3f} | {c['write_wall_p95_ms']:.6f} | {c['compaction_calls_during_writes']} |

Clock limitation: the benchmark uses a module-local synthetic monotonic clock advancing one second per logical write. It covers cadence boundaries deterministically but does not represent scheduler contention or host clock behavior.
"""
    Path(args.report).write_text(report, encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if equivalent else 2


if __name__ == "__main__":
    raise SystemExit(main())
