from __future__ import annotations

import os
import pwd
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .state_db import (
    _check_existing_file,
    _check_existing_sidecars,
    _check_owner_mode,
    _configure,
    _prepare_directory,
    _secure_file,
    _secure_sidecars,
)


WEB_DB_PATH = Path("/var/lib/game-control-web/web.db")

_WEB_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS web_sessions (
        id TEXT PRIMARY KEY,
        actor TEXT NOT NULL,
        csrf_token TEXT NOT NULL,
        created_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(created_at) = 1),
        expires_at TEXT NOT NULL CHECK (is_rfc3339_timestamp(expires_at) = 1)
    )
    """,
)


class WebDatabase:
    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.connection = connection
        self.path = path

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "WebDatabase":
        requested = Path(path)
        if requested.absolute() != WEB_DB_PATH.absolute():
            raise PermissionError("web database path is not approved")
        uid, gid = cls._owner_ids()
        if requested.parent.is_symlink():
            raise PermissionError(f"symlink is not allowed: {requested.parent}")
        _prepare_directory(requested.parent, uid, gid)
        _check_owner_mode(requested.parent, uid, gid, 0o700)
        _check_existing_file(requested, uid, gid)
        _check_existing_sidecars(requested, uid, gid)
        connection = sqlite3.connect(requested, timeout=5.0)
        _configure(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            _migrate_web(connection)
            connection.commit()
            _secure_file(requested, uid, gid)
            _secure_sidecars(requested, uid, gid)
        except Exception:
            connection.rollback()
            connection.close()
            raise
        return cls(connection, requested)

    @staticmethod
    def _owner_ids() -> tuple[int, int]:
        try:
            record = pwd.getpwnam("gamecontrol")
        except KeyError as exc:
            raise PermissionError("gamecontrol account is not installed") from exc
        return record.pw_uid, record.pw_gid

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


def _migrate_web(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version >= 1:
        return
    for statement in _WEB_MIGRATIONS:
        connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
