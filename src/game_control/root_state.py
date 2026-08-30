"""Typed read-only projections of root-owned controller state."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class RootStateRead(Generic[T]):
    """A bounded result that distinguishes unavailable state from a value."""

    available: bool
    value: T | None = None
    reason: str = ""

    @classmethod
    def unavailable(cls, reason: str) -> "RootStateRead[T]":
        return cls(False, None, reason[:128])


def _connection(database: Any) -> sqlite3.Connection | Any | None:
    connection = getattr(database, "connection", database)
    return connection if hasattr(connection, "execute") else None


def _profile_key(profile_id: Any) -> str:
    value = getattr(profile_id, "value", profile_id)
    return str(value)


class RootActiveJobsReader:
    """Read active lifecycle jobs from the injected root state connection."""

    def __init__(self, database: Any):
        self.database = database

    def read(self, profile_id: Any) -> RootStateRead[str | None]:
        connection = _connection(self.database)
        if connection is None:
            return RootStateRead.unavailable("root state is unavailable")
        try:
            row = connection.execute(
                "SELECT operation FROM jobs WHERE profile_id=? AND state IN ('accepted','running') "
                "ORDER BY created_at DESC LIMIT 1",
                (_profile_key(profile_id),),
            ).fetchone()
        except Exception:
            return RootStateRead.unavailable("root jobs are unreadable")
        if row is None:
            return RootStateRead(True, None)
        if not isinstance(row[0], str) or not row[0]:
            return RootStateRead.unavailable("root job state is malformed")
        return RootStateRead(True, row[0])

    def any_active(self) -> RootStateRead[bool]:
        connection = _connection(self.database)
        if connection is None:
            return RootStateRead.unavailable("root state is unavailable")
        try:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE state IN ('accepted','running') LIMIT 1"
            ).fetchone()
        except Exception:
            return RootStateRead.unavailable("root jobs are unreadable")
        return RootStateRead(True, row is not None)

    def __call__(self, profile_id: Any) -> str | None:
        """Compatibility projection used by ``StatusService``."""
        result = self.read(profile_id)
        return result.value if result.available else None


class RootGenerationReader:
    """Read the controller generation without owning its increment operation."""

    def __init__(self, database: Any):
        self.database = database

    def read(self) -> RootStateRead[int]:
        connection = _connection(self.database)
        if connection is None:
            return RootStateRead.unavailable("root state is unavailable")
        try:
            row = connection.execute("PRAGMA application_id").fetchone()
            value = row[0] if row else 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return RootStateRead.unavailable("root generation is malformed")
        except Exception:
            return RootStateRead.unavailable("root generation is unreadable")
        return RootStateRead(True, value)

    def __call__(self) -> int:
        """Compatibility projection used by status snapshots."""
        result = self.read()
        return result.value if result.available and result.value is not None else 0


__all__ = ["RootActiveJobsReader", "RootGenerationReader", "RootStateRead"]
