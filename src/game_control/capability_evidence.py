"""Root-owned evidence for wake-sensitive maintenance decisions."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any


@dataclass(frozen=True)
class WakeSafetyEvidence:
    """A bounded, typed result for the root wake-admission safety check."""

    available: bool
    clear: bool
    reason: str = ""


class RootWakeSafetyEvidence:
    """Read only root-owned jobs and reservation evidence.

    The web capability database is deliberately not opened here. A reservation
    path that exists but cannot be parsed is unsafe, as is any valid active
    reservation: maintenance must acquire the same durable reservation before
    starting.
    """

    def __init__(self, database: Any, reservation_store: Any):
        self.database = database
        self.reservation_store = reservation_store

    def __call__(self) -> WakeSafetyEvidence:
        try:
            connection = getattr(self.database, "connection", self.database)
            if not hasattr(connection, "execute"):
                return WakeSafetyEvidence(False, False, "root state is unavailable")
            active = connection.execute(
                "SELECT 1 FROM jobs WHERE state IN ('accepted','running') LIMIT 1"
            ).fetchone()
            if active is not None:
                return WakeSafetyEvidence(True, False, "root operation is active")

            path = getattr(self.reservation_store, "reservation_path", None)
            if path is None:
                return WakeSafetyEvidence(False, False, "root reservation is unavailable")
            reservation_path = Path(path)
            try:
                info = os.lstat(reservation_path)
            except FileNotFoundError:
                return WakeSafetyEvidence(True, True)
            except OSError:
                return WakeSafetyEvidence(False, False, "root reservation is unreadable")
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return WakeSafetyEvidence(False, False, "root reservation is unsafe")
            if reservation_path.exists():
                reader = getattr(self.reservation_store, "read", None)
                reservation = reader() if callable(reader) else None
                if reservation is None:
                    return WakeSafetyEvidence(False, False, "root reservation is malformed")
                return WakeSafetyEvidence(True, False, "root reservation is present")
            return WakeSafetyEvidence(False, False, "root reservation is unreadable")
        except Exception:
            return WakeSafetyEvidence(False, False, "root wake evidence is unavailable")


__all__ = ["RootWakeSafetyEvidence", "WakeSafetyEvidence"]
