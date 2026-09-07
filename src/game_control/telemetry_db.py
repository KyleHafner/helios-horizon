"""Small, isolated SQLite store for bounded process-resource telemetry."""

from __future__ import annotations

import sqlite3
import os
import queue
import stat
import threading
import time
import math
import json
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping


TELEMETRY_SCHEMA_VERSION = 2
RESOURCE_METRICS = ("cpu_percent", "rss_bytes", "disk_read_bps", "disk_write_bps")
_STATES = ("available", "inactive", "unavailable")
RAW_RETENTION_MS = 48 * 60 * 60 * 1000
ROLLUP_RETENTION_MS = 365 * 24 * 60 * 60 * 1000
ROLLUP_HOUR_MS = 60 * 60 * 1000
COMPACTION_CADENCE_SECONDS = 60.0

# The registry is deliberately finite and contains no identity-bearing labels.
_CONTROLLED_METRICS = {
    **{metric: ("percent" if metric == "cpu_percent" else "bytes" if metric == "rss_bytes" else "bytes_per_second", "gauge") for metric in RESOURCE_METRICS},
    "players": ("players", "gauge"), "tps": ("tps", "gauge"), "mspt": ("milliseconds", "gauge"),
    "cycle_avg_ms": ("milliseconds", "gauge"), "cycle_max_ms": ("milliseconds", "gauge"), "cycle_p95_ms": ("milliseconds", "gauge"),
    "rpc_avg_ms": ("milliseconds", "gauge"), "rpc_max_ms": ("milliseconds", "gauge"), "rpc_p95_ms": ("milliseconds", "gauge"),
    "mspt_p50": ("milliseconds", "gauge"), "mspt_p95": ("milliseconds", "gauge"),
    "mspt_p99": ("milliseconds", "gauge"), "tick_ms_bucket": ("ticks", "histogram"),
    "tick_histogram_le_5": ("ticks", "histogram"), "tick_histogram_le_10": ("ticks", "histogram"),
    "tick_histogram_le_20": ("ticks", "histogram"), "tick_histogram_le_50": ("ticks", "histogram"),
    "tick_histogram_gt_50": ("ticks", "histogram"), "gc_pause": ("milliseconds", "observation"),
    "service_io_read_bytes_total": ("bytes", "counter"), "service_io_write_bytes_total": ("bytes", "counter"),
    "service_io_read_ops_total": ("operations", "counter"), "service_io_write_ops_total": ("operations", "counter"),
    "host_network_rx_bytes_total": ("bytes", "counter"), "host_network_tx_bytes_total": ("bytes", "counter"),
    "host_psi_io_some_avg10": ("percent", "gauge"), "host_psi_io_full_avg10": ("percent", "gauge"),
    "host_disk_read_io_time_ms_total": ("milliseconds", "counter"),
    "host_disk_write_io_time_ms_total": ("milliseconds", "counter"),
    "wake_duration": ("milliseconds", "duration"), "stop_duration": ("milliseconds", "duration"),
    "switch_duration": ("milliseconds", "duration"), "backup_quiesce_duration": ("milliseconds", "duration"),
}


def _create_v2_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE telemetry_series (
        series_id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        metric TEXT NOT NULL,
        unit TEXT NOT NULL,
        kind TEXT NOT NULL,
        labels_json TEXT NOT NULL DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        UNIQUE(profile_id, metric, labels_json))""")
    connection.execute("""CREATE TABLE telemetry_samples (
        series_id TEXT NOT NULL REFERENCES telemetry_series(series_id), ts_ms INTEGER NOT NULL,
        value REAL, state TEXT NOT NULL CHECK(state IN ('available','inactive','unavailable')),
        expected INTEGER NOT NULL CHECK(expected IN (0,1)),
        PRIMARY KEY(series_id, ts_ms),
        CHECK((state='inactive' AND value IS NULL AND expected=0) OR
              (state='unavailable' AND value IS NULL AND expected=1) OR
              (state='available' AND value IS NOT NULL AND expected=1)))""")
    connection.execute("""CREATE TABLE telemetry_rollups (
        series_id TEXT NOT NULL REFERENCES telemetry_series(series_id),
        bucket_start_ms INTEGER NOT NULL,
        min REAL NOT NULL, max REAL NOT NULL, sum REAL NOT NULL,
        count INTEGER NOT NULL CHECK(count > 0),
        PRIMARY KEY(series_id, bucket_start_ms))""")
    connection.execute("""CREATE TABLE telemetry_state_rollups (
        series_id TEXT NOT NULL REFERENCES telemetry_series(series_id),
        bucket_start_ms INTEGER NOT NULL,
        available_ms INTEGER NOT NULL DEFAULT 0, inactive_ms INTEGER NOT NULL DEFAULT 0,
        unavailable_ms INTEGER NOT NULL DEFAULT 0,
        available_count INTEGER NOT NULL DEFAULT 0, inactive_count INTEGER NOT NULL DEFAULT 0,
        unavailable_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(series_id, bucket_start_ms))""")
    connection.execute("""CREATE TABLE telemetry_counter_baselines (
        series_id TEXT PRIMARY KEY REFERENCES telemetry_series(series_id),
        last_value REAL, last_state TEXT NOT NULL CHECK(last_state IN ('available','inactive','unavailable')))""")
    connection.execute("""CREATE TABLE telemetry_state_baselines (
        series_id TEXT PRIMARY KEY REFERENCES telemetry_series(series_id),
        last_ts_ms INTEGER NOT NULL, last_state TEXT NOT NULL CHECK(last_state IN ('available','inactive','unavailable')))""")
    connection.execute("""CREATE VIEW resource_samples AS
        SELECT s.series_id,r.profile_id,s.ts_ms,r.metric,s.value,s.state,s.expected
        FROM telemetry_samples s JOIN telemetry_series r ON r.series_id=s.series_id""")
    connection.execute("CREATE INDEX telemetry_samples_ts ON telemetry_samples(ts_ms)")
    connection.execute("CREATE INDEX telemetry_samples_series_ts ON telemetry_samples(series_id,ts_ms)")
    connection.execute("CREATE INDEX telemetry_rollups_start ON telemetry_rollups(bucket_start_ms)")
    connection.execute("CREATE INDEX telemetry_state_rollups_start ON telemetry_state_rollups(bucket_start_ms)")


def _require_canonical_v2(connection: sqlite3.Connection) -> None:
    expected = {
        "telemetry_series": ("series_id", "profile_id", "metric", "unit", "kind", "labels_json", "active"),
        "telemetry_samples": ("series_id", "ts_ms", "value", "state", "expected"),
        "telemetry_rollups": ("series_id", "bucket_start_ms", "min", "max", "sum", "count"),
        "telemetry_state_rollups": ("series_id", "bucket_start_ms", "available_ms", "inactive_ms", "unavailable_ms", "available_count", "inactive_count", "unavailable_count"),
        "telemetry_counter_baselines": ("series_id", "last_value", "last_state"),
        "telemetry_state_baselines": ("series_id", "last_ts_ms", "last_state"),
    }
    for table, columns in expected.items():
        info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = tuple(row[1] for row in info)
        if actual != columns:
            raise RuntimeError("malformed telemetry schema")
        if any(str(row[2]).upper() not in {"TEXT", "INTEGER", "REAL"} for row in info):
            raise RuntimeError("malformed telemetry schema")
    sample_info = {row[1]: row for row in connection.execute("PRAGMA table_info(telemetry_samples)")}
    rollup_info = {row[1]: row for row in connection.execute("PRAGMA table_info(telemetry_rollups)")}
    state_info = {row[1]: row for row in connection.execute("PRAGMA table_info(telemetry_state_rollups)")}
    if any(sample_info[name][3] != 1 for name in ("series_id", "ts_ms", "state", "expected")):
        raise RuntimeError("malformed telemetry schema")
    if any(rollup_info[name][3] != 1 for name in ("series_id", "bucket_start_ms", "min", "max", "sum", "count")):
        raise RuntimeError("malformed telemetry schema")
    if any(state_info[name][3] != 1 for name in ("series_id", "bucket_start_ms", "available_ms", "inactive_ms", "unavailable_ms", "available_count", "inactive_count", "unavailable_count")):
        raise RuntimeError("malformed telemetry schema")
    sample_pk = {row[1]: row[5] for row in connection.execute("PRAGMA table_info(telemetry_samples)")}
    rollup_pk = {row[1]: row[5] for row in connection.execute("PRAGMA table_info(telemetry_rollups)")}
    sample_fk = connection.execute("PRAGMA foreign_key_list(telemetry_samples)").fetchall()
    rollup_fk = connection.execute("PRAGMA foreign_key_list(telemetry_rollups)").fetchall()
    state_fk = connection.execute("PRAGMA foreign_key_list(telemetry_state_rollups)").fetchall()
    if sample_pk.get("series_id") != 1 or sample_pk.get("ts_ms") != 2:
        raise RuntimeError("malformed telemetry schema")
    if rollup_pk.get("series_id") != 1 or rollup_pk.get("bucket_start_ms") != 2:
        raise RuntimeError("malformed telemetry schema")
    if not all(any(row[2] == "telemetry_series" and row[3] == "series_id" for row in rows)
               for rows in (sample_fk, rollup_fk, state_fk)):
        raise RuntimeError("malformed telemetry schema")
    view = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='resource_samples'"
    ).fetchone()
    if view != ("view",):
        raise RuntimeError("malformed telemetry schema")
    samples_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='telemetry_samples'").fetchone()[0] or ""
    rollups_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='telemetry_rollups'").fetchone()[0] or ""
    if "CHECK" not in samples_sql.upper() or "CHECK" not in rollups_sql.upper():
        raise RuntimeError("malformed telemetry schema")


def _validate_profile(profile_id: Any) -> str:
    key = str(getattr(profile_id, "value", profile_id))
    if not key or len(key) > 128 or not all(char.isascii() and (char.isalnum() or char in "_.:-") for char in key):
        raise ValueError("invalid telemetry profile id")
    return key


def _validate_metric(metric: Any) -> str:
    if not isinstance(metric, str) or metric not in _CONTROLLED_METRICS:
        raise ValueError("unsupported telemetry metric")
    return metric


def _canonical_bucket(value: Any) -> str:
    if value == "+Inf":
        return "+Inf"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid telemetry bucket")
    try:
        number = float(value)
    except ValueError:
        raise ValueError("invalid telemetry bucket") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid telemetry bucket")
    return format(number, ".15g")


def _validate_labels(labels: Mapping[str, str] | None) -> tuple[dict[str, str], str]:
    if labels is None:
        normalized: dict[str, str] = {}
    elif not isinstance(labels, Mapping):
        raise ValueError("invalid telemetry labels")
    else:
        normalized = {}
        for key, value in labels.items():
            if not isinstance(key, str) or not isinstance(value, str) or key not in {"source", "bucket", "result"}:
                raise ValueError("invalid telemetry labels")
            if len(key) > 32 or len(value) > 64:
                raise ValueError("invalid telemetry labels")
            normalized[key] = _canonical_bucket(value) if key == "bucket" else value
    if "source" in normalized and normalized["source"] not in {"prometheus", "rcon", "procfs", "systemd", "slotd"}:
        raise ValueError("invalid telemetry source")
    if "result" in normalized and normalized["result"] not in {"success", "failure", "timeout", "cancelled"}:
        raise ValueError("invalid telemetry result")
    encoded = json.dumps(dict(sorted(normalized.items())), separators=(",", ":"))
    return normalized, encoded


def _compact_connection(connection: sqlite3.Connection, *, now_ms: int, raw_retention_ms: int) -> dict[str, int]:
    raw_cutoff = now_ms - raw_retention_ms
    ordinary_rows = connection.execute(
        """SELECT series_id,(ts_ms / ?) * ?,MIN(value),MAX(value),SUM(value),COUNT(*)
           FROM telemetry_samples JOIN telemetry_series USING(series_id)
           WHERE ts_ms < ? AND state='available'
             AND kind!='counter'
             AND metric NOT IN ('mspt_p50','mspt_p95','mspt_p99')
           GROUP BY series_id,(ts_ms / ?)""",
        (ROLLUP_HOUR_MS, ROLLUP_HOUR_MS, raw_cutoff, ROLLUP_HOUR_MS),
    ).fetchall()
    # Cumulative counters are reduced to reset-aware positive interval deltas.
    # A reset contributes the new post-reset value; an inactive/unavailable
    # sample breaks continuity so no activity is fabricated across a gap.
    counter_rows: list[tuple[str, int, float, float, float, int]] = []
    for (series_id,) in connection.execute("SELECT series_id FROM telemetry_series WHERE kind='counter'"):
        samples = connection.execute(
            "SELECT ts_ms,value,state FROM telemetry_samples WHERE series_id=? AND ts_ms<? ORDER BY ts_ms",
            (series_id, raw_cutoff),
        ).fetchall()
        baseline = connection.execute(
            "SELECT last_value,last_state FROM telemetry_counter_baselines WHERE series_id=?", (series_id,)
        ).fetchone()
        previous: float | None = float(baseline[0]) if baseline and baseline[1] == "available" and baseline[0] is not None else None
        grouped: dict[int, list[float]] = defaultdict(list)
        for ts_ms, value, sample_state in samples:
            if sample_state != "available" or value is None:
                previous = None
                continue
            current = float(value)
            if previous is not None:
                delta = current - previous if current >= previous else current
                if math.isfinite(delta) and delta >= 0:
                    grouped[(int(ts_ms) // ROLLUP_HOUR_MS) * ROLLUP_HOUR_MS].append(delta)
            previous = current
        if samples:
            _last_ts, last_value, last_state = samples[-1]
            connection.execute(
                """INSERT INTO telemetry_counter_baselines(series_id,last_value,last_state) VALUES(?,?,?)
                   ON CONFLICT(series_id) DO UPDATE SET last_value=excluded.last_value,last_state=excluded.last_state""",
                (series_id, last_value if last_state == "available" else None, last_state),
            )
        for bucket, deltas in grouped.items():
            counter_rows.append((str(series_id), bucket, min(deltas), max(deltas), sum(deltas), len(deltas)))
    rows = ordinary_rows + counter_rows
    connection.executemany(
        """INSERT INTO telemetry_rollups
           (series_id,bucket_start_ms,min,max,sum,count) VALUES(?,?,?,?,?,?)
           ON CONFLICT(series_id,bucket_start_ms) DO UPDATE SET
             min=MIN(min,excluded.min), max=MAX(max,excluded.max),
             sum=sum+excluded.sum, count=count+excluded.count""",
        rows,
    )
    state_rows: list[tuple[Any, ...]] = []
    for (series_id,) in connection.execute("SELECT series_id FROM telemetry_series"):
        samples = connection.execute(
            "SELECT ts_ms,state FROM telemetry_samples WHERE series_id=? AND ts_ms<? ORDER BY ts_ms",
            (series_id, raw_cutoff),
        ).fetchall()
        baseline = connection.execute(
            "SELECT last_ts_ms,last_state FROM telemetry_state_baselines WHERE series_id=?", (series_id,)
        ).fetchone()
        timeline = ([] if baseline is None else [(int(baseline[0]), str(baseline[1]), False)])
        timeline.extend((int(ts), str(sample_state), True) for ts, sample_state in samples)
        following = connection.execute(
            "SELECT ts_ms FROM telemetry_samples WHERE series_id=? AND ts_ms>=? ORDER BY ts_ms LIMIT 1",
            (series_id, raw_cutoff),
        ).fetchone()
        durations: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for index, (ts_ms, sample_state, is_sample) in enumerate(timeline):
            bucket = (int(ts_ms) // ROLLUP_HOUR_MS) * ROLLUP_HOUR_MS
            if is_sample:
                counts[bucket][str(sample_state)] += 1
            end = int(timeline[index + 1][0]) if index + 1 < len(timeline) else (
                min(raw_cutoff, int(following[0])) if following is not None else raw_cutoff
            )
            cursor = int(ts_ms)
            while cursor < end:
                span_bucket = (cursor // ROLLUP_HOUR_MS) * ROLLUP_HOUR_MS
                boundary = min(end, span_bucket + ROLLUP_HOUR_MS)
                durations[span_bucket][str(sample_state)] += boundary - cursor
                cursor = boundary
        if timeline:
            connection.execute(
                """INSERT INTO telemetry_state_baselines(series_id,last_ts_ms,last_state) VALUES(?,?,?)
                   ON CONFLICT(series_id) DO UPDATE SET last_ts_ms=excluded.last_ts_ms,last_state=excluded.last_state""",
                (series_id, raw_cutoff, timeline[-1][1]),
            )
        for bucket in sorted(set(durations) | set(counts)):
            state_rows.append((
                str(series_id), bucket,
                durations[bucket]["available"], durations[bucket]["inactive"], durations[bucket]["unavailable"],
                counts[bucket]["available"], counts[bucket]["inactive"], counts[bucket]["unavailable"],
            ))
    connection.executemany(
        """INSERT INTO telemetry_state_rollups
           (series_id,bucket_start_ms,available_ms,inactive_ms,unavailable_ms,available_count,inactive_count,unavailable_count)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(series_id,bucket_start_ms) DO UPDATE SET
             available_ms=available_ms+excluded.available_ms,
             inactive_ms=inactive_ms+excluded.inactive_ms,
             unavailable_ms=unavailable_ms+excluded.unavailable_ms,
             available_count=available_count+excluded.available_count,
             inactive_count=inactive_count+excluded.inactive_count,
             unavailable_count=unavailable_count+excluded.unavailable_count""",
        state_rows,
    )
    raw_deleted = connection.execute("DELETE FROM telemetry_samples WHERE ts_ms < ?", (raw_cutoff,)).rowcount
    rollups_deleted = connection.execute(
        "DELETE FROM telemetry_rollups WHERE bucket_start_ms < ?", (now_ms - ROLLUP_RETENTION_MS,)
    ).rowcount
    connection.execute("DELETE FROM telemetry_state_rollups WHERE bucket_start_ms < ?", (now_ms - ROLLUP_RETENTION_MS,))
    return {"hours_compacted": len(rows), "raw_deleted": raw_deleted, "rollups_deleted": rollups_deleted}


class TelemetryDatabase:
    """A separate, disposable database; lifecycle durability is not changed.

    Writes run retention maintenance on the first write and after each
    ``COMPACTION_CADENCE_SECONDS`` elapsed since successful cleanup. A quiet
    database can therefore retain expired raw rows until the next write or an
    explicit ``compact_hourly`` call; close persists queued writes without a
    forced sweep.
    """

    def __init__(self, connection: sqlite3.Connection, path: Path, *, retention_ms: int = RAW_RETENTION_MS,
                 queue_size: int = 32):
        self.connection = connection
        self.path = path
        self.retention_ms = max(60_000, int(retention_ms))
        self._last_write_ms: int | None = None
        self._last_committed_ts_ms: int | None = None
        self._last_compaction_monotonic: float | None = None
        self._last_compaction_now_ms: int | None = None
        self._compaction_lock = threading.Lock()
        self._last_error: str | None = None
        self._queue_size = max(1, int(queue_size))
        self._pending: dict[str, tuple[Any, ...]] = {}
        self._inflight: dict[str, tuple[Any, ...]] = {}
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=self._queue_size)
        self._queue_lock = threading.Lock()
        self._state = "running"
        self._terminal_event = threading.Event()
        self._close_result: dict[str, Any] | None = None
        self._accepted = 0
        self._abandoned = 0
        self._active_tokens = 0
        self._reconciled_active_tokens = 0
        self._coalesced = 0
        self._dropped = 0
        self._written = 0
        self._worker = threading.Thread(target=self._writer_loop, name="horizon-telemetry-writer", daemon=True)
        self._worker.start()

    @classmethod
    def open(cls, path: str | Path, *, retention_ms: int = RAW_RETENTION_MS,
             queue_size: int = 32) -> "TelemetryDatabase":
        target = Path(path)
        parent = target.parent
        if not parent.exists() or not parent.is_dir() or parent.is_symlink():
            raise PermissionError("telemetry database parent is not a secured directory")
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.geteuid() or parent_stat.st_gid != os.getegid() or parent_stat.st_mode & 0o022:
            raise PermissionError("telemetry database parent ownership or mode is insecure")
        try:
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise PermissionError("telemetry database parent cannot be securely opened") from exc
        try:
            parent_identity = os.fstat(parent_fd)
            current_parent = os.stat(parent, follow_symlinks=False)
            if (parent_identity.st_dev, parent_identity.st_ino) != (current_parent.st_dev, current_parent.st_ino):
                raise PermissionError("telemetry database parent changed during secure open")
        finally:
            os.close(parent_fd)
        sidecar_identities: dict[str, tuple[int, int] | None] = {}
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                raise PermissionError("telemetry database sidecar is not a safe regular file")
            if sidecar.exists():
                info = os.stat(sidecar, follow_symlinks=False)
                sidecar_identities[suffix] = (info.st_dev, info.st_ino)
            else:
                sidecar_identities[suffix] = None
        try:
            fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise PermissionError("telemetry database target is not a safe regular file") from exc
        identity = os.fstat(fd)
        os.close(fd)
        current = os.stat(target, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise PermissionError("telemetry database target changed during secure open")
        _secure_file(target)
        current_parent = os.stat(parent, follow_symlinks=False)
        if (current_parent.st_dev, current_parent.st_ino) != (parent_stat.st_dev, parent_stat.st_ino):
            raise PermissionError("telemetry database parent changed during secure open")
        for suffix, expected in sidecar_identities.items():
            sidecar = Path(f"{target}{suffix}")
            if expected is None:
                if sidecar.is_symlink():
                    raise PermissionError("telemetry database sidecar changed during secure open")
            elif not sidecar.exists() or sidecar.is_symlink():
                raise PermissionError("telemetry database sidecar changed during secure open")
            else:
                info = os.stat(sidecar, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != expected:
                    raise PermissionError("telemetry database sidecar changed during secure open")
        connection = sqlite3.connect(target)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.set_authorizer(_deny_attach)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > TELEMETRY_SCHEMA_VERSION:
                raise RuntimeError("unsupported future telemetry schema")
            if version == TELEMETRY_SCHEMA_VERSION:
                _require_canonical_v2(connection)
            else:
                legacy = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='resource_samples'"
                ).fetchone()
                if version == 1 and not legacy:
                    raise RuntimeError("malformed v1 telemetry schema")
                if version == 0 and legacy:
                    raise RuntimeError("malformed unversioned telemetry schema")
                connection.execute("BEGIN IMMEDIATE")
                if version == 1:
                    old_columns = {row[1] for row in connection.execute("PRAGMA table_info(resource_samples)")}
                    if not {"profile_id", "ts_ms", "metric", "value", "state"} <= old_columns:
                        raise RuntimeError("malformed telemetry migration schema")
                    rows = connection.execute(
                        "SELECT profile_id, ts_ms, metric, value, state FROM resource_samples"
                    ).fetchall()
                    connection.execute("ALTER TABLE resource_samples RENAME TO resource_samples_legacy")
                else:
                    rows = []
                _create_v2_schema(connection)
                for profile_id, ts_ms, metric, value, state in rows:
                    try:
                        key = _validate_profile(profile_id)
                        _validate_metric(metric)
                    except ValueError as exc:
                        raise RuntimeError("malformed telemetry migration row") from exc
                    if state not in _STATES or not isinstance(ts_ms, int) or ts_ms < 0:
                        raise RuntimeError("malformed telemetry migration row")
                    derived = "available" if state == "available" and value is not None else (
                        "inactive" if state == "inactive" else "unavailable"
                    )
                    if derived == "available" and (
                        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    ):
                        raise RuntimeError("malformed telemetry migration value")
                    sid = f"resource.{key}.{metric}"
                    unit, kind = _CONTROLLED_METRICS[metric]
                    connection.execute(
                        "INSERT OR IGNORE INTO telemetry_series(series_id,profile_id,metric,unit,kind,labels_json) VALUES(?,?,?,?,?,'{}')",
                        (sid, key, metric, unit, kind),
                    )
                    metadata = connection.execute(
                        "SELECT profile_id,metric,unit,kind,labels_json FROM telemetry_series WHERE series_id=?", (sid,)
                    ).fetchone()
                    if metadata != (key, metric, unit, kind, "{}"):
                        raise RuntimeError("telemetry series metadata collision")
                    connection.execute(
                        "INSERT INTO telemetry_samples VALUES(?,?,?,?,?)",
                        (sid, ts_ms, value if derived == "available" else None, derived, int(derived != "inactive")),
                    )
                if version == 1:
                    connection.execute("DROP TABLE resource_samples_legacy")
                connection.execute(f"PRAGMA user_version = {TELEMETRY_SCHEMA_VERSION}")
                connection.commit()
                _require_canonical_v2(connection)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
            raise
        _secure_file(target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{target}{suffix}")
            if sidecar.exists():
                _secure_file(sidecar)
        return cls(connection, target, retention_ms=retention_ms, queue_size=queue_size)

    @staticmethod
    def series_id(profile_id: Any, metric: str) -> str:
        key = _validate_profile(profile_id)
        metric = _validate_metric(metric)
        return f"resource.{key}.{metric}"

    def _register_series(self, connection: sqlite3.Connection, profile_id: Any, metric: str,
                         labels: Mapping[str, str] | None = None) -> str:
        key = _validate_profile(profile_id)
        metric = _validate_metric(metric)
        _, labels_json = _validate_labels(labels)
        digest = hashlib.sha256(labels_json.encode()).hexdigest()[:16]
        series_id = self.series_id(profile_id, metric) + (".l" + digest if labels_json != "{}" else "")
        unit, kind = _CONTROLLED_METRICS[metric]
        connection.execute(
            "INSERT OR IGNORE INTO telemetry_series(series_id, profile_id, metric, unit, kind, labels_json) VALUES (?, ?, ?, ?, ?, ?)",
            (series_id, key, metric, unit, kind, labels_json),
        )
        actual = connection.execute(
            "SELECT profile_id,metric,unit,kind,labels_json FROM telemetry_series WHERE series_id=?", (series_id,)
        ).fetchone()
        if actual != (key, metric, unit, kind, labels_json):
            raise RuntimeError("telemetry series metadata collision")
        return series_id

    def record_sample(self, profile_id: Any, metric: str, value: float | int | None, *, ts_ms: int,
                      state: str = "available", labels: Mapping[str, str] | None = None) -> None:
        """Record one controlled metric without persisting player identity."""
        _validate_profile(profile_id)
        _validate_metric(metric)
        _validate_labels(labels)
        if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0:
            raise ValueError("invalid telemetry sample")
        if state not in _STATES:
            raise ValueError("invalid telemetry state")
        if state == "inactive":
            value, expected = None, 0
        elif state == "unavailable":
            value, expected = None, 1
        else:
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("available telemetry requires finite numeric value")
            expected = 1
        with self._compaction_lock:
            with self.connection:
                series_id = self._register_series(self.connection, profile_id, metric, labels)
                self.connection.execute("""INSERT INTO telemetry_samples(series_id, ts_ms, value, state, expected)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(series_id, ts_ms) DO UPDATE SET value=excluded.value, state=excluded.state, expected=excluded.expected""",
                    (series_id, ts_ms, value, state, expected))
                compaction_now_ms = self._maybe_compact(self.connection, now_ms=ts_ms)
            if compaction_now_ms is not None:
                self._last_compaction_monotonic = time.monotonic()
                self._last_compaction_now_ms = compaction_now_ms
            self._last_committed_ts_ms = max(ts_ms, self._last_committed_ts_ms or 0)

    def query_samples(self, profile_id: Any, metric: str, *, since_ms: int = 0, limit: int = 1000,
                      labels: Mapping[str, str] | None = None) -> list[tuple[int, float | None, str]]:
        _validate_profile(profile_id)
        _validate_metric(metric)
        _, label_json = _validate_labels(labels)
        if (not isinstance(since_ms, int) or isinstance(since_ms, bool) or since_ms < 0
                or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10000):
            raise ValueError("invalid telemetry query")
        label_suffix = "" if label_json == "{}" else ".l" + hashlib.sha256(label_json.encode()).hexdigest()[:16]
        sid = self.series_id(profile_id, metric) + label_suffix
        metadata = self.connection.execute(
            "SELECT profile_id,metric,unit,kind,labels_json FROM telemetry_series WHERE series_id=?", (sid,)
        ).fetchone()
        if metadata is None:
            return []
        expected = (_validate_profile(profile_id), metric, *_CONTROLLED_METRICS[metric], label_json)
        if metadata != expected:
            raise RuntimeError("telemetry series metadata collision")
        return self.connection.execute("SELECT ts_ms, value, state FROM telemetry_samples WHERE series_id=? AND ts_ms>=? ORDER BY ts_ms LIMIT ?", (sid, since_ms, limit)).fetchall()

    def record_rollup(self, profile_id: Any, metric: str, *, bucket_start_ms: int,
                      minimum: float, maximum: float, total: float, count: int,
                      bucket: str | int | float | None = None,
                      labels: Mapping[str, str] | None = None) -> None:
        """Upsert mergeable aggregates; percentiles intentionally are not stored."""
        normalized, _ = _validate_labels(labels)
        if bucket is not None:
            if "bucket" in normalized:
                raise ValueError("duplicate telemetry bucket")
            normalized["bucket"] = _canonical_bucket(bucket)
        if not isinstance(bucket_start_ms, int) or isinstance(bucket_start_ms, bool) or bucket_start_ms < 0:
            raise ValueError("invalid telemetry timestamp")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError("invalid telemetry count")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in (minimum, maximum, total)) or minimum > maximum:
            raise ValueError("invalid telemetry rollup")
        with self.connection:
            series_id = self._register_series(self.connection, profile_id, metric, normalized)
            self.connection.execute("""INSERT INTO telemetry_rollups(series_id, bucket_start_ms, min, max, sum, count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id, bucket_start_ms) DO UPDATE SET
                  min=MIN(min, excluded.min), max=MAX(max, excluded.max),
                  sum=sum + excluded.sum, count=count + excluded.count""",
                (series_id, bucket_start_ms, minimum, maximum, total, count))

    def compact_hourly(self, *, now_ms: int) -> dict[str, int]:
        """Compact expired raw values into mergeable hourly aggregates.

        Scalar percentiles are deliberately not materialized: min/max/sum/count
        remain mergeable, while histogram buckets retain their label-isolated
        series. Re-running a completed hour is idempotent.
        """
        if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
            raise ValueError("invalid telemetry timestamp")
        with self._compaction_lock:
            with self.connection:
                effective_now_ms = max(now_ms, self._last_compaction_now_ms or 0, self._last_committed_ts_ms or 0)
                result = _compact_connection(self.connection, now_ms=effective_now_ms, raw_retention_ms=self.retention_ms)
            self._last_compaction_monotonic = time.monotonic()
            self._last_compaction_now_ms = effective_now_ms
            return result

    def _maybe_compact(self, connection: sqlite3.Connection, *, now_ms: int) -> int | None:
        """Compact only when the monotonic cadence has elapsed.

        The caller must hold ``_compaction_lock`` and must update the marker
        only after its surrounding transaction commits successfully.
        """
        current = time.monotonic()
        previous = self._last_compaction_monotonic
        if previous is not None and current - previous < COMPACTION_CADENCE_SECONDS:
            return None
        effective_now_ms = max(now_ms, self._last_compaction_now_ms or 0, self._last_committed_ts_ms or 0)
        _compact_connection(connection, now_ms=effective_now_ms, raw_retention_ms=self.retention_ms)
        return effective_now_ms

    def enqueue_process_sample(self, profile_id: Any, sample: Any | None, *, ts_ms: int, state: str) -> bool:
        """Queue a sample without performing SQLite work on the caller thread.

        At most one pending sample per profile is retained.  A newer sample
        coalesces an older one; a full queue drops the new sample and records
        backpressure in health rather than blocking status or slotd.
        """
        self._validate_sample(sample, ts_ms=ts_ms, state=state)
        key = "process:" + _validate_profile(profile_id)
        return self._enqueue(key, ("process", profile_id, sample, ts_ms, state), timestamp_index=3)

    def enqueue_sample(self, profile_id: Any, metric: str, value: float | int | None, *, ts_ms: int,
                       state: str = "available", labels: Mapping[str, str] | None = None) -> bool:
        """Nonblocking generic metric enqueue using the bounded writer queue."""
        profile = _validate_profile(profile_id)
        metric = _validate_metric(metric)
        normalized, labels_json = _validate_labels(labels)
        if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0 or state not in _STATES:
            raise ValueError("invalid telemetry sample")
        if state == "available":
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("available telemetry requires finite numeric value")
        else:
            value = None
        suffix = hashlib.sha256(labels_json.encode()).hexdigest()[:16]
        key = f"sample:{profile}:{metric}:{suffix}"
        item = ("sample", profile, metric, value, ts_ms, state, normalized)
        return self._enqueue(key, item, timestamp_index=4)

    def _enqueue(self, key: str, item: tuple[Any, ...], *, timestamp_index: int) -> bool:
        with self._queue_lock:
            if self._state != "running":
                self._last_error = self._last_error or "WriterClosed"
                return False
            if key in self._pending or key in self._inflight:
                current = self._pending.get(key) or self._inflight.get(key)
                if current is not None and item[timestamp_index] <= current[timestamp_index]:
                    self._coalesced += 1
                    self._accepted += 1
                    return True
                self._pending[key] = item
                self._coalesced += 1
                self._accepted += 1
                return True
            if len(self._pending) + len(self._inflight) >= self._queue_size:
                self._dropped += 1
                self._last_error = "Backpressure"
                return False
            try:
                self._queue.put_nowait(key)
            except queue.Full:
                self._dropped += 1
                self._last_error = "Backpressure"
                return False
            self._pending[key] = item
            self._accepted += 1
            return True

    def record_process_sample(
        self,
        profile_id: Any,
        sample: Any | None,
        *,
        ts_ms: int,
        state: str,
    ) -> None:
        self._validate_sample(sample, ts_ms=ts_ms, state=state)
        key = _validate_profile(profile_id)
        values: Mapping[str, Any] = {
            "cpu_percent": getattr(sample, "cpu_percent", None) if sample is not None else None,
            "rss_bytes": getattr(sample, "rss_bytes", None) if sample is not None else None,
            "disk_read_bps": getattr(sample, "disk_read_bps", None) if sample is not None else None,
            "disk_write_bps": getattr(sample, "disk_write_bps", None) if sample is not None else None,
        }
        with self._compaction_lock:
            with self.connection:
                rows = []
                for metric, value in values.items():
                    series_id = self._register_series(self.connection, key, metric)
                    row_state = "inactive" if state == "inactive" else ("unavailable" if state == "unavailable" else ("available" if value is not None else "unavailable"))
                    rows.append((series_id, ts_ms, value if row_state == "available" else None, row_state, int(row_state != "inactive")))
                self.connection.executemany(
                    """INSERT INTO telemetry_samples(series_id, ts_ms, value, state, expected)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(series_id, ts_ms) DO UPDATE SET
                         value=excluded.value, state=excluded.state,
                         expected=excluded.expected""", rows,
                )
                compaction_now_ms = self._maybe_compact(self.connection, now_ms=ts_ms)
            if compaction_now_ms is not None:
                self._last_compaction_monotonic = time.monotonic()
                self._last_compaction_now_ms = compaction_now_ms
            self._last_committed_ts_ms = max(ts_ms, self._last_committed_ts_ms or 0)
        with self._queue_lock:
            self._last_write_ms = ts_ms
            self._last_error = None

    def _validate_sample(self, sample: Any | None, *, ts_ms: int, state: str) -> None:
        if not isinstance(state, str) or state not in ("available", "inactive", "unavailable"):
            raise ValueError("invalid telemetry state")
        if not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0:
            raise ValueError("invalid telemetry timestamp")
        if sample is None:
            return
        for metric in RESOURCE_METRICS:
            value = getattr(sample, metric, None)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("invalid telemetry value")

    def _writer_loop(self) -> None:
        worker_connection: sqlite3.Connection | None = None
        try:
            worker_connection = sqlite3.connect(self.path)
            worker_connection.execute("PRAGMA busy_timeout = 5000")
            worker_connection.execute("PRAGMA journal_mode = WAL")
            worker_connection.execute("PRAGMA synchronous = NORMAL")
            worker_connection.execute("PRAGMA foreign_keys = ON")
            worker_connection.set_authorizer(_deny_attach)
            while True:
                try:
                    key = self._queue.get(timeout=0.2)
                except queue.Empty:
                    with self._queue_lock:
                        if self._state in {"closed", "failed"}:
                            break
                    continue
                if key is None:
                    self._queue.task_done()
                    break
                with self._queue_lock:
                    self._active_tokens += 1
                    item = self._pending.pop(key, None)
                    if item is not None:
                        self._inflight[key] = item
                if item is None:
                    with self._queue_lock:
                        self._active_tokens -= 1
                    self._queue.task_done()
                    continue
                try:
                    while item is not None:
                        try:
                            if item[0] == "process":
                                self._write_with(worker_connection, item[1], sample=item[2], ts_ms=item[3], state=item[4])
                                written_ts = item[3]
                            else:
                                self._write_generic_with(
                                    worker_connection, item[1], item[2], item[3],
                                    ts_ms=item[4], state=item[5], labels=item[6],
                                )
                                written_ts = item[4]
                            with self._queue_lock:
                                self._written += 1
                                self._last_write_ms = written_ts
                                self._last_error = None
                        except Exception as error:
                            self.record_failure(error)
                        with self._queue_lock:
                            item = self._pending.pop(key, None)
                            if item is None:
                                self._inflight.pop(key, None)
                            else:
                                self._inflight[key] = item
                finally:
                    with self._queue_lock:
                        self._active_tokens -= 1
                        reconciled = self._reconciled_active_tokens > 0
                        if reconciled:
                            self._reconciled_active_tokens -= 1
                    if not reconciled:
                        self._queue.task_done()
        except BaseException as error:
            self._mark_terminal(error)
        finally:
            if worker_connection is not None:
                worker_connection.close()

    def _mark_terminal(self, error: BaseException) -> None:
        with self._queue_lock:
            if self._state in {"closed", "failed"}:
                return
            self._state = "failed"
            self._last_error = type(error).__name__[:64]
            self._abandon_locked()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
            self._terminal_event.set()

    def _abandon_locked(self) -> None:
        self._abandoned += max(0, self._accepted - self._written - self._abandoned)
        self._pending.clear()
        self._inflight.clear()

    def _reconcile_queue_locked(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
        for _ in range(self._active_tokens - self._reconciled_active_tokens):
            self._queue.task_done()
        self._reconciled_active_tokens = self._active_tokens

    def _write_with(self, connection: sqlite3.Connection, profile_id: Any, *, sample: Any | None, ts_ms: int, state: str) -> None:
        key = _validate_profile(profile_id)
        values = {
            "cpu_percent": getattr(sample, "cpu_percent", None) if sample is not None else None,
            "rss_bytes": getattr(sample, "rss_bytes", None) if sample is not None else None,
            "disk_read_bps": getattr(sample, "disk_read_bps", None) if sample is not None else None,
            "disk_write_bps": getattr(sample, "disk_write_bps", None) if sample is not None else None,
        }
        with self._compaction_lock:
            with connection:
                rows = []
                for metric, value in values.items():
                    series_id = self._register_series(connection, key, metric)
                    row_state = "inactive" if state == "inactive" else ("unavailable" if state == "unavailable" else ("available" if value is not None else "unavailable"))
                    rows.append((series_id, ts_ms, value if row_state == "available" else None, row_state, int(row_state != "inactive")))
                connection.executemany(
                    """INSERT INTO telemetry_samples(series_id, ts_ms, value, state, expected)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(series_id, ts_ms) DO UPDATE SET
                         value=excluded.value, state=excluded.state,
                         expected=excluded.expected""", rows,
                )
                compaction_now_ms = self._maybe_compact(connection, now_ms=ts_ms)
            if compaction_now_ms is not None:
                self._last_compaction_monotonic = time.monotonic()
                self._last_compaction_now_ms = compaction_now_ms
            self._last_committed_ts_ms = max(ts_ms, self._last_committed_ts_ms or 0)

    def _write_generic_with(self, connection: sqlite3.Connection, profile_id: Any, metric: str,
                            value: float | int | None, *, ts_ms: int, state: str,
                            labels: Mapping[str, str]) -> None:
        with self._compaction_lock:
            with connection:
                series_id = self._register_series(connection, profile_id, metric, labels)
                expected = int(state != "inactive")
                connection.execute(
                    """INSERT INTO telemetry_samples(series_id,ts_ms,value,state,expected) VALUES(?,?,?,?,?)
                       ON CONFLICT(series_id,ts_ms) DO UPDATE SET
                         value=excluded.value,state=excluded.state,expected=excluded.expected""",
                    (series_id, ts_ms, value if state == "available" else None, state, expected),
                )
                compaction_now_ms = self._maybe_compact(connection, now_ms=ts_ms)
            if compaction_now_ms is not None:
                self._last_compaction_monotonic = time.monotonic()
                self._last_compaction_now_ms = compaction_now_ms
            self._last_committed_ts_ms = max(ts_ms, self._last_committed_ts_ms or 0)

    def drain(self, timeout: float | None = None) -> bool:
        """Wait for all accepted writes; intended for deterministic lifecycle close/tests."""
        if self._state == "closed" and not self._worker.is_alive():
            return True
        done = threading.Event()
        def waiter() -> None:
            self._queue.join()
            done.set()
        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)

    def health(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        age = None if self._last_write_ms is None else max(0, now - self._last_write_ms)
        with self._queue_lock:
            pending = len(set(self._pending) | set(self._inflight))
            state = self._state
            closed = state == "closed"
            result = dict(self._close_result) if self._close_result is not None else None
            error = self._last_error
            age = None if self._last_write_ms is None else max(0, now - self._last_write_ms)
            return {"ok": error is None and state == "running", "last_sample_age_ms": age,
                    "last_error": error, "pending": pending, "queue_capacity": self._queue_size,
                    "coalesced": self._coalesced, "dropped": self._dropped, "written": self._written,
                    "accepted": self._accepted, "abandoned": self._abandoned, "closed": closed,
                    "state": state, "close_result": result}

    def effective_pragmas(self) -> dict[str, int | str]:
        """Return connection-local durability settings without exposing paths or rows."""
        return {
            "journal_mode": str(self.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            "synchronous": int(self.connection.execute("PRAGMA synchronous").fetchone()[0]),
            "user_version": int(self.connection.execute("PRAGMA user_version").fetchone()[0]),
        }

    def record_failure(self, error: BaseException) -> None:
        with self._queue_lock:
            self._last_error = type(error).__name__[:64]

    def close(self) -> dict[str, Any]:
        with self._queue_lock:
            if self._close_result is not None:
                return dict(self._close_result)
            if self._state == "running":
                self._state = "closing"
            failed = self._state == "failed"
        drained = failed or self.drain(timeout=10.0)
        if not drained:
            with self._queue_lock:
                self._abandon_locked()
                self._reconcile_queue_locked()
                self._state = "closed"
                self._last_error = self._last_error or "DrainTimeout"
                self._terminal_event.set()
            result = {"closed": True, "drained": False, "abandoned": True, "worker_alive": self._worker.is_alive()}
        else:
            if not failed and self._worker.is_alive():
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    with self._queue_lock:
                        self._abandon_locked()
                        self._reconcile_queue_locked()
                        self._state = "closed"
                        self._last_error = "CloseSentinelFull"
                        self._terminal_event.set()
                    result = {"closed": True, "drained": False, "abandoned": True, "worker_alive": True}
                else:
                    self._worker.join(timeout=10.0)
                    alive = self._worker.is_alive()
                    with self._queue_lock:
                        if alive:
                            self._abandon_locked()
                            self._reconcile_queue_locked()
                            self._last_error = "CloseTimeout"
                        self._state = "closed"
                        self._terminal_event.set()
                    result = {"closed": True, "drained": not alive, "abandoned": alive, "worker_alive": alive}
            else:
                with self._queue_lock:
                    self._state = "closed"
                    self._terminal_event.set()
                result = {"closed": True, "drained": True, "abandoned": self._abandoned > 0, "worker_alive": self._worker.is_alive()}
        with self._queue_lock:
            self._close_result = result
        self.connection.close()
        return dict(result)


def _deny_attach(action: int, *_args: Any) -> int:
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


def _secure_file(path: Path) -> None:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid():
        raise PermissionError("telemetry database file ownership or type is insecure")
    os.chmod(path, 0o600)


__all__ = ["COMPACTION_CADENCE_SECONDS", "RAW_RETENTION_MS", "RESOURCE_METRICS", "TELEMETRY_SCHEMA_VERSION", "TelemetryDatabase"]
