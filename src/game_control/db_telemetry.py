"""Bounded, identity-free SQLite retention and growth telemetry."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import PerfAggregate


_DB_TELEMETRY_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="horizon-db-telemetry")


# Keep this list deliberately closed: telemetry must never enumerate or expose
# arbitrary tables, values, actors, profile ids, or session identifiers.
_TABLES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "state": (
        ("events", "timestamp"), ("audit", "timestamp"), ("jobs", "created_at"),
        ("confirmations", "expires_at"), ("backups", "created_at"),
        ("backup_protections", None), ("notification_rules", None),
        ("notification_deliveries", "delivered_at"), ("updates", "created_at"),
        ("rpc_idempotency", "created_at"), ("player_sessions", "started_at"),
        ("metric_samples", "ts"), ("benchmark_runs", "created_at"),
    ),
    "telemetry": (
        ("telemetry_series", None), ("telemetry_samples", "ts_ms"),
        ("telemetry_rollups", "bucket_start_ms"),
        ("telemetry_state_rollups", "bucket_start_ms"),
        ("telemetry_counter_baselines", None), ("telemetry_state_baselines", "last_ts_ms"),
    ),
    "web": (("web_sessions", "created_at"),),
}


def _aggregate(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    p95 = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return {"count": len(ordered), "avg_ms": sum(ordered) / len(ordered), "p95_ms": p95, "max_ms": ordered[-1]}


def _timestamp(value: Any, column: str) -> str | None:
    if value is None:
        return None
    if column.endswith("_ms"):
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def collect_database_telemetry(database: Any, kind: str) -> dict[str, Any]:
    """Collect a small fixed query set, failing closed when a DB is inactive."""
    tables = _TABLES.get(kind)
    if tables is None:
        raise ValueError("unknown database telemetry kind")
    path = getattr(database, "path", None)
    owned_connection = False
    connection = None
    if path is not None and str(path) != ":memory:":
        try:
            # Never move a live writer connection across threads. Production
            # wrappers expose an approved path; this read-only handle is
            # short-lived and has a bounded busy timeout.
            connection = sqlite3.connect(
                f"file:{Path(path)}?mode=ro", uri=True, timeout=0.2,
                check_same_thread=False,
            )
            connection.execute("PRAGMA query_only=ON")
            owned_connection = True
        except (OSError, sqlite3.DatabaseError):
            return {"state": "unavailable", "tables": (), "page_count": None, "page_size": None,
                    "freelist_pages": None, "wal_bytes": None, "query_ms": _aggregate([])}
    else:
        connection = getattr(database, "connection", database)
    if not isinstance(connection, sqlite3.Connection):
        return {"state": "inactive", "tables": (), "page_count": None, "page_size": None,
                "freelist_pages": None, "wal_bytes": None, "query_ms": _aggregate([])}
    samples: list[float] = []
    result: list[dict[str, Any]] = []
    try:
        for table, timestamp_column in tables:
            started = time.monotonic()
            quoted = '"' + table.replace('"', '""') + '"'
            row_count = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
            oldest = None
            if timestamp_column is not None:
                column = '"' + timestamp_column.replace('"', '""') + '"'
                oldest = _timestamp(connection.execute(f"SELECT MIN({column}) FROM {quoted}").fetchone()[0], timestamp_column)
            samples.append((time.monotonic() - started) * 1000.0)
            result.append({"name": table, "row_count": row_count, "oldest_timestamp": oldest})
        page_count = max(0, int(connection.execute("PRAGMA page_count").fetchone()[0]))
        page_size = max(0, int(connection.execute("PRAGMA page_size").fetchone()[0]))
        freelist = max(0, int(connection.execute("PRAGMA freelist_count").fetchone()[0]))
    except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError):
        return {"state": "unavailable", "tables": (), "page_count": None, "page_size": None,
                "freelist_pages": None, "wal_bytes": None, "query_ms": _aggregate(samples)}
    finally:
        if owned_connection:
            connection.close()
    wal_bytes = None
    if path is not None and str(path) != ":memory:":
        try:
            wal_bytes = Path(f"{path}-wal").stat().st_size
        except (FileNotFoundError, OSError):
            wal_bytes = 0
    return {"state": "available", "tables": tuple(result), "page_count": page_count,
            "page_size": page_size, "freelist_pages": freelist, "wal_bytes": wal_bytes,
            "query_ms": _aggregate(samples)}


def collect_perf_databases(*, state: Any, telemetry: Any | None = None, web: Any | None = None) -> dict[str, Any]:
    return {
        "state": collect_database_telemetry(state, "state"),
        "telemetry": collect_database_telemetry(telemetry, "telemetry"),
        "web": collect_database_telemetry(web, "web"),
    }


async def collect_perf_databases_async(
    *, state: Any, telemetry: Any | None = None, web: Any | None = None
) -> dict[str, Any]:
    """Collect path-backed databases off-loop; retain direct memory test seams."""
    async def one(database: Any, kind: str) -> dict[str, Any]:
        path = getattr(database, "path", None)
        if path is not None and str(path) != ":memory:":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _DB_TELEMETRY_EXECUTOR, collect_database_telemetry, database, kind
            )
        # Injected :memory: connections may be thread-affine. They are a test
        # seam only; production wrappers always provide approved paths.
        return collect_database_telemetry(database, kind)

    state_result, telemetry_result, web_result = await asyncio.gather(
        one(state, "state"), one(telemetry, "telemetry"), one(web, "web")
    )
    return {"state": state_result, "telemetry": telemetry_result, "web": web_result}


async def collect_database_telemetry_async(database: Any, kind: str) -> dict[str, Any]:
    """Async single-database variant used by the web process."""
    path = getattr(database, "path", None)
    if path is not None and str(path) != ":memory:":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _DB_TELEMETRY_EXECUTOR, collect_database_telemetry, database, kind
        )
    return collect_database_telemetry(database, kind)


__all__ = [
    "collect_database_telemetry",
    "collect_perf_databases",
    "collect_perf_databases_async",
    "collect_database_telemetry_async",
]
