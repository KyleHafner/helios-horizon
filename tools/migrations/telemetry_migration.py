"""Offline, idempotent importer for lifecycle metric_samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from game_control.telemetry_db import (
    _CONTROLLED_METRICS,
    _create_v2_schema,
    _require_canonical_v2,
    _validate_profile,
)

MIGRATION_VERSION = 2
METRIC_MAP = {"players": "players", "tps": "tps", "mspt": "mspt",
              "perf.cpu_percent": "cpu_percent", "perf.rss_bytes": "rss_bytes",
              "perf.disk_read_bps": "disk_read_bps", "perf.disk_write_bps": "disk_write_bps",
              "perf.cycle_avg_ms": "cycle_avg_ms", "perf.cycle_max_ms": "cycle_max_ms", "perf.cycle_p95_ms": "cycle_p95_ms",
              "perf.rpc_avg_ms": "rpc_avg_ms", "perf.rpc_max_ms": "rpc_max_ms", "perf.rpc_p95_ms": "rpc_p95_ms"}

# The state database owns these objects outside metric_samples.  They are part
# of the canonical v3 image captured from the live controller and therefore
# must be admitted explicitly; checking only the metric index incorrectly
# rejects a legitimate live source.  Keep the SQL exact (after whitespace
# normalization) so a lookalike object cannot pass this boundary.
_STATE_V3_INDEX_SQL = {
    "idx_backup_protections_scope": (
        "CREATE INDEX idx_backup_protections_scope ON backup_protections("
        "profile_id, destination_id, backup_class, remote_verified, comparison_state)"
    ),
    "idx_benchmark_runs_profile": (
        "CREATE INDEX idx_benchmark_runs_profile ON benchmark_runs(profile_id, created_at DESC)"
    ),
    "idx_metric_samples": "CREATE INDEX idx_metric_samples ON metric_samples(profile_id, metric, ts)",
    "idx_player_sessions_profile": (
        "CREATE INDEX idx_player_sessions_profile ON player_sessions(profile_id, started_at)"
    ),
}
_STATE_V3_TRIGGER_SQL = {
    "audit_append_only_delete": (
        "CREATE TRIGGER audit_append_only_delete BEFORE DELETE ON audit BEGIN "
        "SELECT RAISE(ABORT, 'audit is append-only'); END"
    ),
    "audit_append_only_update": (
        "CREATE TRIGGER audit_append_only_update BEFORE UPDATE ON audit BEGIN "
        "SELECT RAISE(ABORT, 'audit is append-only'); END"
    ),
    "events_append_only_delete": (
        "CREATE TRIGGER events_append_only_delete BEFORE DELETE ON events BEGIN "
        "SELECT RAISE(ABORT, 'events are append-only'); END"
    ),
    "events_append_only_update": (
        "CREATE TRIGGER events_append_only_update BEFORE UPDATE ON events BEGIN "
        "SELECT RAISE(ABORT, 'events are append-only'); END"
    ),
}


class MigrationError(RuntimeError):
    pass


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _secure_db(path: Path, *, writable: bool) -> None:
    if path.is_symlink() or not path.is_file():
        if writable and not path.exists():
            return
        raise MigrationError("database path must be a regular non-symlink file")
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_gid != os.getegid() or (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1:
        raise MigrationError("database ownership or mode is insecure")


def _reject_symlinked_parents(path: Path) -> None:
    current = path.parent
    while current != current.parent:
        if current.is_symlink():
            raise MigrationError("database parent must not contain symlinks")
        current = current.parent


def _reject_same_file(left: Path, right: Path, label: str) -> None:
    try:
        if os.path.samefile(left, right):
            raise MigrationError(f"{label} source and backup must be distinct files")
    except FileNotFoundError:
        raise MigrationError(f"{label} source or backup is missing") from None


def _reject_wal_sidecars(path: Path) -> None:
    """Require a self-contained SQLite image for offline migration.

    A byte-for-byte copy of the main file is not a consistent snapshot while
    SQLite's WAL or shared-memory sidecars are present.  The migration is
    intentionally offline, so fail closed instead of attempting to reconcile
    those files.
    """
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if os.path.lexists(sidecar):
            raise MigrationError(f"SQLite {suffix[1:].upper()} sidecar is present")


def _secure_target_parent(path: Path) -> None:
    parent = path.parent
    if not parent.exists() or parent.is_symlink() or not parent.is_dir():
        raise MigrationError("target parent is unavailable")
    info = parent.stat()
    if info.st_uid != os.geteuid() or info.st_gid != os.getegid() or info.st_mode & 0o077:
        raise MigrationError("target parent ownership or mode is insecure")


def _writer_present() -> bool:
    markers = ("game-slotd", "game-control-web", "game_control.controller", "game_control.slotd_main")
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "ignore")
        except OSError:
            continue
        if any(marker in command for marker in markers):
            return True
    return False


def _source_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
    table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metric_samples'").fetchone()
    if table is None:
        raise MigrationError("source metric_samples table is missing")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(metric_samples)"))
    if columns != ("profile_id", "metric", "ts", "value"):
        raise MigrationError("source metric_samples schema is malformed")
    rows = connection.execute("SELECT profile_id, metric, ts, value FROM metric_samples ORDER BY profile_id, metric, ts").fetchall()
    checked = []
    seen: set[tuple[str, str, str]] = set()
    for profile, metric, ts, value in rows:
        if not isinstance(profile, str) or not profile or len(profile) > 128 or not isinstance(metric, str) or metric not in METRIC_MAP:
            raise MigrationError("source contains an unsupported metric row")
        if not isinstance(ts, str) or not ts or len(ts) > 64 or "\x00" in ts:
            raise MigrationError("source contains a malformed timestamp")
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.isoformat() == "":
                raise ValueError
        except (TypeError, ValueError, OverflowError) as exc:
            raise MigrationError("source contains a malformed timestamp") from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise MigrationError("source contains a malformed metric value")
        key = (profile, METRIC_MAP[metric], ts)
        if key in seen:
            raise MigrationError("source contains duplicate metric timestamp")
        seen.add(key)
        checked.append((profile, METRIC_MAP[metric], datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(), float(value), "available", 1))
    return checked


def _telemetry_rows(connection: sqlite3.Connection) -> list[tuple[str, str, str, float]]:
    info = tuple(row[1] for row in connection.execute("PRAGMA table_info(resource_samples)"))
    if info != ("profile_id", "ts_ms", "metric", "value", "state"):
        raise MigrationError("source telemetry resource_samples schema is malformed")
    rows = []
    for profile, ts_ms, metric, value, state in connection.execute("SELECT profile_id,ts_ms,metric,value,state FROM resource_samples"):
        expected = 0 if state == "inactive" else 1
        valid_value = (state == "available" and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))) or (state in {"inactive", "unavailable"} and value is None)
        try:
            valid_profile = _validate_profile(profile)
        except (TypeError, ValueError):
            valid_profile = None
        if valid_profile is None or metric not in {"cpu_percent", "rss_bytes", "disk_read_bps", "disk_write_bps"} or state not in {"available", "inactive", "unavailable"} or not isinstance(ts_ms, int) or isinstance(ts_ms, bool) or ts_ms < 0 or ts_ms > 32503680000000 or not valid_value:
            raise MigrationError("source telemetry row is malformed or unsupported")
        rows.append((profile, metric, datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(), None if value is None else float(value), state, expected))
    return rows


def _validate_sqlite_source(connection: sqlite3.Connection) -> None:
    if (connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA user_version").fetchone()[0] != 3):
        raise MigrationError("source or backup SQLite integrity check failed")
    table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='metric_samples'").fetchone()
    if table is None:
        raise MigrationError("source or backup metric_samples table is missing")
    info = tuple((row[1], row[2].upper(), row[3], row[5]) for row in connection.execute("PRAGMA table_info(metric_samples)"))
    if info != (("profile_id", "TEXT", 1, 0), ("metric", "TEXT", 1, 0),
                ("ts", "TEXT", 1, 0), ("value", "REAL", 1, 0)):
        raise MigrationError("source metric_samples schema is malformed")
    normalize = lambda value: "".join(value.lower().split())
    sql_row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_samples'").fetchone()
    expected_sql = ("createtablemetric_samples(profile_idtextnotnull,metrictextnotnull"
                    "check(metricin('tps','mspt','players')ormetriclike'perf.%'),"
                    "tstextnotnullcheck(is_rfc3339_timestamp(ts)=1),valuerealnotnull)")
    if not sql_row or normalize(sql_row[0]) != expected_sql:
        raise MigrationError("source metric_samples table definition is malformed")
    objects = {
        name: normalize(sql)
        for kind, name, sql in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('index', 'trigger') AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected = {
        **{name: normalize(sql) for name, sql in _STATE_V3_INDEX_SQL.items()},
        **{name: normalize(sql) for name, sql in _STATE_V3_TRIGGER_SQL.items()},
    }
    if objects.keys() != expected.keys():
        raise MigrationError("source state v3 has unexpected indexes or triggers")
    for name, expected_sql in expected.items():
        if objects[name] != expected_sql:
            raise MigrationError(f"source state v3 object {name} is malformed")


def _validate_sqlite_telemetry_source(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise MigrationError("source telemetry version or integrity is invalid")
    info = tuple((row[1], row[2].upper(), row[3], row[5]) for row in connection.execute("PRAGMA table_info(resource_samples)"))
    if info != (("profile_id", "TEXT", 1, 1), ("ts_ms", "INTEGER", 1, 2),
                ("metric", "TEXT", 1, 3), ("value", "REAL", 0, 0),
                ("state", "TEXT", 1, 0)):
        raise MigrationError("source telemetry schema is malformed")
    sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='resource_samples'").fetchone()
    normalized = "".join(sql[0].lower().split()) if sql else ""
    expected_sql = ("createtableresource_samples(profile_idtextnotnull,ts_msintegernotnull,"
                    "metrictextnotnullcheck(metricin('cpu_percent','rss_bytes','disk_read_bps','disk_write_bps')),"
                    "valuereal,statetextnotnullcheck(statein('available','inactive','unavailable')),"
                    "primarykey(profile_id,ts_ms,metric))")
    if normalized != expected_sql:
        raise MigrationError("source telemetry table definition is malformed")
    index_row = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='resource_samples_ts'").fetchone()
    if not index_row or "".join(index_row[0].lower().split()) != "createindexresource_samples_tsonresource_samples(ts_ms)":
        raise MigrationError("source telemetry index is malformed")
    extras = connection.execute("SELECT name FROM sqlite_master WHERE type IN ('index','trigger') AND name NOT LIKE 'sqlite_%'").fetchall()
    if {row[0] for row in extras} != {"resource_samples_ts"}:
        raise MigrationError("source telemetry has unexpected indexes or triggers")


def _target_state(connection: sqlite3.Connection) -> str:
    rows = connection.execute("SELECT series_id,ts_ms,value,state,expected FROM telemetry_samples ORDER BY series_id,ts_ms").fetchall()
    payload = repr(rows).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _imported_state(connection: sqlite3.Connection, rows: list[tuple[str, str, str, float]]) -> str:
    expected = []
    for profile, metric, ts, value, state, expected_flag in rows:
        ts_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        expected.append((f"resource.{profile}.{metric}", ts_ms, value, state, expected_flag))
    actual = []
    for series_id, ts_ms, value, state, expected_flag in expected:
        row = connection.execute("SELECT series_id,ts_ms,value,state,expected FROM telemetry_samples WHERE series_id=? AND ts_ms=?", (series_id, ts_ms)).fetchone()
        if row != (series_id, ts_ms, value, state, expected_flag):
            raise MigrationError("imported telemetry row binding mismatch")
        actual.append(row)
    return hashlib.sha256(repr(actual).encode("utf-8")).hexdigest()


def _validate_series_bindings(connection: sqlite3.Connection, rows: list[tuple[str, str, str, float]]) -> None:
    for profile, metric, _ts, _value, _state, _expected in rows:
        unit, kind = _CONTROLLED_METRICS[metric]
        sid = f"resource.{profile}.{metric}"
        actual = connection.execute(
            "SELECT profile_id,metric,unit,kind,labels_json,active FROM telemetry_series WHERE series_id=?", (sid,)
        ).fetchone()
        if actual != (profile, metric, unit, kind, "{}", 1):
            raise MigrationError("telemetry series metadata binding mismatch")


def _restore_target(path: Path, preimage: Path | None) -> None:
    """Restore using an exact private file, never a predictable temp path."""
    _secure_target_parent(path)
    if path.is_symlink():
        raise MigrationError("target became a symlink during recovery")
    if preimage is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _secure_db(preimage, writable=False)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.restore-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination, preimage.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def migrate(source: str | Path, target: str | Path, backup: str | Path, *, dry_run: bool = False,
            writer_probe=_writer_present, telemetry_source: str | Path | None = None, telemetry_backup: str | Path | None = None) -> dict[str, Any]:
    source_path, target_path, backup_path = Path(source), Path(target), Path(backup)
    _secure_db(source_path, writable=False)
    _secure_db(backup_path, writable=False)
    _reject_symlinked_parents(source_path); _reject_symlinked_parents(backup_path)
    _reject_same_file(source_path, backup_path, "state")
    _reject_wal_sidecars(source_path)
    _reject_wal_sidecars(backup_path)
    _secure_target_parent(target_path)
    if target_path.is_symlink():
        raise MigrationError("target database must not be a symlink")
    _reject_wal_sidecars(target_path)
    _reject_symlinked_parents(target_path)
    if target_path.exists():
        _reject_same_file(target_path, source_path, "target/state")
        _reject_same_file(target_path, backup_path, "target/state")
    if writer_probe():
        raise MigrationError("lifecycle writer is active; migration is offline-only")
    telemetry_path = Path(telemetry_source) if telemetry_source is not None else None
    telemetry_backup_path = Path(telemetry_backup) if telemetry_backup is not None else None
    if (telemetry_path is None) != (telemetry_backup_path is None):
        raise MigrationError("telemetry source and backup must be supplied together")
    if telemetry_path is not None:
        _secure_db(telemetry_path, writable=False); _secure_db(telemetry_backup_path, writable=False)
        _reject_symlinked_parents(telemetry_path); _reject_symlinked_parents(telemetry_backup_path)
        _reject_same_file(telemetry_path, telemetry_backup_path, "telemetry")
        if target_path.exists():
            _reject_same_file(target_path, telemetry_path, "target/telemetry")
            _reject_same_file(target_path, telemetry_backup_path, "target/telemetry")
    if target_path.exists():
        _secure_db(target_path, writable=True)
        try:
            with sqlite3.connect(f"file:{target_path}?mode=ro", uri=True) as existing_target:
                existing_target.execute("PRAGMA query_only=ON")
                try:
                    _require_canonical_v2(existing_target)
                except RuntimeError as exc:
                    raise MigrationError("target telemetry schema is malformed") from exc
                if existing_target.execute("PRAGMA user_version").fetchone()[0] != 2:
                    raise MigrationError("target telemetry schema version is not canonical")
        except sqlite3.DatabaseError as exc:
            raise MigrationError("target telemetry database is malformed") from exc
    source_sha = _fingerprint(source_path)
    backup_sha = _fingerprint(backup_path)
    if source_sha != backup_sha:
        raise MigrationError("backup is not an exact offline source copy")
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as src:
        src.execute("PRAGMA query_only=ON")
        _validate_sqlite_source(src)
        rows = _source_rows(src)
    with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as backup_db:
        backup_db.execute("PRAGMA query_only=ON")
        _validate_sqlite_source(backup_db)
    if _fingerprint(source_path) != source_sha or _fingerprint(backup_path) != backup_sha or os.path.samefile(source_path, backup_path):
        raise MigrationError("state source changed during preflight")
    state_row_count = len(rows)
    telemetry_sha = None
    telemetry_backup_sha = None
    telemetry_row_count = 0
    if telemetry_path is not None:
        telemetry_sha = _fingerprint(telemetry_path)
        telemetry_backup_sha = _fingerprint(telemetry_backup_path)
        if telemetry_sha != telemetry_backup_sha:
            raise MigrationError("telemetry backup is not an exact offline source copy")
        with sqlite3.connect(f"file:{telemetry_path}?mode=ro", uri=True) as telemetry_db:
            telemetry_db.execute("PRAGMA query_only=ON"); _validate_sqlite_telemetry_source(telemetry_db); telemetry_rows = _telemetry_rows(telemetry_db); telemetry_row_count = len(telemetry_rows); rows.extend(telemetry_rows)
        with sqlite3.connect(f"file:{telemetry_backup_path}?mode=ro", uri=True) as telemetry_db:
            telemetry_db.execute("PRAGMA query_only=ON"); _validate_sqlite_telemetry_source(telemetry_db)
        if _fingerprint(telemetry_path) != telemetry_sha or _fingerprint(telemetry_backup_path) != telemetry_backup_sha:
            raise MigrationError("telemetry source changed during preflight")
    for checked_path in (source_path, backup_path, telemetry_path, telemetry_backup_path):
        if checked_path is not None:
            _reject_wal_sidecars(checked_path)
    unique = {}
    for row in rows:
        key = (row[0], row[1], int(datetime.fromisoformat(row[2].replace("Z", "+00:00")).timestamp() * 1000))
        prior = unique.get(key)
        if prior is not None and prior[3:] != row[3:]:
            raise MigrationError("state and telemetry domains contain a conflicting metric sample")
        unique[key] = row
    rows = sorted(unique.values(), key=lambda row: (row[0], row[1], row[2], row[4], row[5]))
    result = {"sourceSha256": source_sha, "sourceRows": state_row_count + telemetry_row_count,
              "stateSourceRows": state_row_count, "telemetrySourceRows": telemetry_row_count,
              "importedRows": len(rows), "telemetrySourceSha256": telemetry_sha,
              "telemetryBackupSha256": telemetry_backup_sha, "target": str(target_path), "dryRun": dry_run}
    if dry_run:
        return result
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        _secure_db(target_path, writable=True)
    elif target_path.parent.stat().st_mode & 0o077:
        raise MigrationError("target parent ownership or mode is insecure")
    preimage: Path | None = None
    if target_path.exists():
        fd, preimage_name = tempfile.mkstemp(prefix=f".{target_path.name}.preimage-", dir=target_path.parent)
        preimage = Path(preimage_name)
        try:
            with os.fdopen(fd, "wb") as destination, target_path.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            os.chmod(preimage, 0o600)
        except BaseException:
            try:
                preimage.unlink()
            except FileNotFoundError:
                pass
            raise
    try:
        connection = sqlite3.connect(target_path)
    except BaseException:
        if preimage is not None:
            try:
                preimage.unlink()
            except FileNotFoundError:
                pass
        raise
    os.chmod(target_path, 0o600)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry_series'").fetchone():
            _create_v2_schema(connection)
            connection.execute("PRAGMA user_version = 2")
        try:
            _require_canonical_v2(connection)
        except RuntimeError as exc:
            raise MigrationError("target telemetry schema is malformed") from exc
        if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise MigrationError("target telemetry schema version is not canonical")
        connection.execute("""CREATE TABLE IF NOT EXISTS migration_ledger (
            source_sha256 TEXT PRIMARY KEY, source_rows INTEGER NOT NULL,
            imported_rows INTEGER NOT NULL, migration_version INTEGER NOT NULL,
            backup_sha256 TEXT NOT NULL, target_sha256 TEXT NOT NULL,
            target_samples INTEGER NOT NULL, target_integrity TEXT NOT NULL,
            telemetry_source_sha256 TEXT, telemetry_backup_sha256 TEXT,
            state_rows INTEGER NOT NULL DEFAULT 0, telemetry_rows INTEGER NOT NULL DEFAULT 0)""")
        ledger_columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(migration_ledger)"))
        if ledger_columns != ("source_sha256", "source_rows", "imported_rows", "migration_version", "backup_sha256", "target_sha256", "target_samples", "target_integrity", "telemetry_source_sha256", "telemetry_backup_sha256", "state_rows", "telemetry_rows"):
            raise MigrationError("migration ledger schema is malformed")
        existing = connection.execute("SELECT source_rows,imported_rows,migration_version,backup_sha256,target_sha256,target_samples,target_integrity,telemetry_source_sha256,telemetry_backup_sha256,state_rows,telemetry_rows FROM migration_ledger WHERE source_sha256=?", (source_sha,)).fetchone()
        if existing is not None:
            if (existing[0] != state_row_count + telemetry_row_count or existing[1] != len(rows) or existing[2] != MIGRATION_VERSION
                    or existing[3] != backup_sha or existing[7] != telemetry_sha or existing[8] != telemetry_backup_sha
                    or existing[9] != state_row_count or existing[10] != telemetry_row_count
                    or existing[4] != _imported_state(connection, rows)):
                raise MigrationError("idempotency ledger binding mismatch")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            samples = connection.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
            if existing[6] != "ok" or integrity != "ok" or samples < existing[5]:
                raise MigrationError("idempotency ledger state mismatch")
            _validate_series_bindings(connection, rows)
            connection.commit()
            result.update(importedRows=existing[1], idempotent=True, targetSha256=existing[4], backupSha256=backup_sha)
            return result
        series = {}
        for profile, metric, ts, value, state, expected_flag in rows:
            sid = f"resource.{profile}.{metric}"
            unit, kind = _CONTROLLED_METRICS[metric]
            connection.execute("INSERT OR IGNORE INTO telemetry_series(series_id,profile_id,metric,unit,kind,labels_json) VALUES(?,?,?,?,?,'{}')", (sid, profile, metric, unit, kind))
            _validate_series_bindings(connection, [(profile, metric, ts, value, state, expected_flag)])
            series[(profile, metric)] = sid
            # Lifecycle timestamps are RFC3339; migration intentionally does not reinterpret them.
            ts_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            try:
                connection.execute("INSERT INTO telemetry_samples(series_id,ts_ms,value,state,expected) VALUES(?,?,?,?,?)", (sid, ts_ms, value, state, expected_flag))
            except sqlite3.IntegrityError as exc:
                raise MigrationError("source row collides with target telemetry sample") from exc
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise MigrationError("target integrity check failed before commit")
        target_samples = connection.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0]
        target_sha_before = _imported_state(connection, rows)
        connection.execute("INSERT INTO migration_ledger(source_sha256,source_rows,imported_rows,migration_version,backup_sha256,target_sha256,target_samples,target_integrity,telemetry_source_sha256,telemetry_backup_sha256,state_rows,telemetry_rows) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (source_sha, state_row_count + telemetry_row_count, len(rows), MIGRATION_VERSION, backup_sha, target_sha_before, target_samples, integrity, telemetry_sha, telemetry_backup_sha, state_row_count, telemetry_row_count))
        connection.commit()
        try:
            with sqlite3.connect(target_path) as verify:
                if verify.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError("target integrity check failed after commit")
                if verify.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0] < target_samples:
                    raise MigrationError("target sample count regressed after commit")
                if _imported_state(verify, rows) != target_sha_before:
                    raise MigrationError("imported target state changed after commit")
        except BaseException:
            _restore_target(target_path, preimage)
            raise
        result.update(importedRows=len(rows), idempotent=False, backupSha256=backup_sha, targetSha256=target_sha_before, targetFileSha256=_fingerprint(target_path), targetSamples=target_samples)
        return result
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        # SQLite rollback protects rows, but the migration contract also
        # covers setup/schema failures after the file has been created. Close
        # before restoring so no descriptor can keep the replacement open.
        connection.close()
        _restore_target(target_path, preimage)
        raise
    finally:
        connection.close()
        if preimage is not None:
            try:
                preimage.unlink()
            except FileNotFoundError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument("--telemetry-source", type=Path)
    parser.add_argument("--telemetry-backup", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = migrate(
            args.source,
            args.target,
            args.backup,
            dry_run=args.dry_run,
            telemetry_source=args.telemetry_source,
            telemetry_backup=args.telemetry_backup,
        )
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0
