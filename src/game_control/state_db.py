from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


STATE_DB_PATH = Path("/var/lib/game-control/state.db")

_STATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL CHECK (is_rfc3339_timestamp(timestamp) = 1),
        profile_id TEXT,
        code TEXT NOT NULL,
        message TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL CHECK (is_rfc3339_timestamp(timestamp) = 1),
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        profile_id TEXT,
        result TEXT NOT NULL,
        error_code TEXT,
        detail TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        profile_id TEXT,
        operation TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(created_at) = 1),
        finished_at TEXT CHECK (finished_at IS NULL OR is_rfc3339_timestamp(finished_at) = 1),
        completion_seq INTEGER,
        detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        id TEXT PRIMARY KEY,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        profile_id TEXT,
        payload TEXT NOT NULL,
        expires_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(expires_at) = 1),
        consumed_at TEXT CHECK (consumed_at IS NULL OR is_rfc3339_timestamp(consumed_at) = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backups (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(created_at) = 1),
        size_bytes INTEGER NOT NULL,
        verified INTEGER NOT NULL,
        protected INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_rules (
        profile_id TEXT NOT NULL,
        event TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        PRIMARY KEY (profile_id, event)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_deliveries (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        event TEXT NOT NULL,
        state_generation INTEGER NOT NULL,
        channel TEXT NOT NULL,
        delivered_at TEXT CHECK (delivered_at IS NULL OR is_rfc3339_timestamp(delivered_at) = 1),
        error_code TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS updates (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(created_at) = 1),
        strategy TEXT NOT NULL,
        prior_version TEXT,
        new_version TEXT,
        state TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rpc_idempotency (
        request_id TEXT PRIMARY KEY,
        canonical_request TEXT NOT NULL,
        response TEXT,
        status TEXT NOT NULL DEFAULT 'completed',
        created_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(created_at) = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_sessions (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        player TEXT NOT NULL,
        started_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(started_at) = 1),
        ended_at TEXT CHECK (ended_at IS NULL OR is_rfc3339_timestamp(ended_at) = 1),
        source TEXT NOT NULL CHECK (source IN ('crafty', 'log', 'recovered'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_samples (
        profile_id TEXT NOT NULL,
        metric TEXT NOT NULL CHECK (metric IN ('tps', 'mspt', 'players') OR metric LIKE 'perf.%'),
        ts TEXT NOT NULL CHECK (is_rfc3339_timestamp(ts) = 1),
        value REAL NOT NULL
    )
    """,
)


class StateDatabase:
    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "StateDatabase":
        requested = Path(path)
        if requested.absolute() != STATE_DB_PATH.absolute():
            raise PermissionError("state database path is not approved")
        if os.geteuid() != 0:
            raise PermissionError("state database requires root")
        _reject_symlink(requested.parent)
        _prepare_directory(requested.parent, 0, 0)
        _check_owner_mode(requested.parent, 0, 0, 0o700)
        _check_existing_file(requested, 0, 0)
        _check_existing_sidecars(requested, 0, 0)
        connection = sqlite3.connect(requested, timeout=5.0)
        _configure(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _migrate_state(connection)
            connection.commit()
            _secure_file(requested, 0, 0)
            _secure_sidecars(requested, 0, 0)
        except Exception:
            connection.rollback()
            connection.close()
            raise
        return cls(connection, requested)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()


def _migrate_state(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        for statement in _STATE_TABLES:
            connection.execute(statement)
    else:
        # CREATE IF NOT EXISTS keeps this migration safe for partially
        # initialized development databases.
        for statement in _STATE_TABLES:
            connection.execute(statement)
    metric_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='metric_samples'"
    ).fetchone()[0] or ""
    if "OR metric LIKE 'perf.%'" not in metric_sql:
        connection.execute("ALTER TABLE metric_samples RENAME TO metric_samples_legacy")
        connection.execute(_STATE_TABLES[-1])
        connection.execute(
            "INSERT INTO metric_samples(profile_id, metric, ts, value) "
            "SELECT profile_id, metric, ts, value FROM metric_samples_legacy"
        )
        connection.execute("DROP TABLE metric_samples_legacy")
    # Existing Task 3 databases predate pending idempotency claims and
    # durable transition generations.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rpc_idempotency)")}
    if "status" not in columns:
        connection.execute("ALTER TABLE rpc_idempotency ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
    confirmation_columns = {row[1] for row in connection.execute("PRAGMA table_info(confirmations)")}
    if "state_generation" not in confirmation_columns:
        connection.execute("ALTER TABLE confirmations ADD COLUMN state_generation INTEGER NOT NULL DEFAULT 0")
    jobs_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    if jobs_columns and "completion_seq" not in jobs_columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN completion_seq INTEGER")
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(completion_seq), 0) FROM jobs"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT rowid FROM jobs "
            "WHERE finished_at IS NOT NULL AND completion_seq IS NULL "
            "ORDER BY julianday(finished_at), julianday(created_at), rowid"
        ).fetchall()
        for (rowid,) in rows:
            next_seq += 1
            connection.execute(
                "UPDATE jobs SET completion_seq=? WHERE rowid=? AND completion_seq IS NULL",
                (next_seq, rowid),
            )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS events_append_only_update "
        "BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS events_append_only_delete "
        "BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_append_only_update "
        "BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS audit_append_only_delete "
        "BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_player_sessions_profile"
        " ON player_sessions(profile_id, started_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_metric_samples"
        " ON metric_samples(profile_id, metric, ts)"
    )
    connection.execute("PRAGMA user_version = 2")


def prune_metric_samples(
    connection: sqlite3.Connection, *, now: str, max_age_days: int = 30
) -> int:
    cursor = connection.execute(
        "DELETE FROM metric_samples"
        " WHERE julianday(ts) < julianday(?) - ?",
        (now, max_age_days),
    )
    return cursor.rowcount


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.create_function("is_rfc3339_timestamp", 1, _is_rfc3339_timestamp, deterministic=True)
    connection.set_authorizer(_deny_attach)


def _deny_attach(action: int, _arg1, _arg2, _db, _source) -> int:
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK


_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_timestamp(value: object) -> int:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.tzinfo is not None and parsed.utcoffset() is not None)


def _prepare_directory(path: Path, uid: int, gid: int) -> None:
    if path.exists():
        return
    path.mkdir(parents=True, mode=0o700)
    os.chown(path, uid, gid)


def _check_owner_mode(path: Path, uid: int, gid: int, mode: int) -> None:
    info = path.stat()
    if info.st_uid != uid or info.st_gid != gid or (info.st_mode & 0o777) != mode:
        raise PermissionError(f"insecure permissions on {path}")


def _check_existing_file(path: Path, uid: int, gid: int) -> None:
    if path.is_symlink():
        raise PermissionError(f"symlink is not allowed: {path}")
    if path.exists():
        _check_owner_mode(path, uid, gid, 0o600)


def _check_existing_sidecars(path: Path, uid: int, gid: int) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        _check_existing_file(sidecar, uid, gid)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PermissionError(f"symlink is not allowed: {path}")


def _secure_file(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    os.chmod(path, 0o600)


def _secure_sidecars(path: Path, uid: int, gid: int) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            _secure_file(sidecar, uid, gid)
