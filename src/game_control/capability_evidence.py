"""Root-owned evidence for wake-sensitive maintenance decisions."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any

from .root_state import RootActiveJobsReader


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
        self.active_jobs = (
            database if isinstance(database, RootActiveJobsReader)
            else RootActiveJobsReader(database)
        )
        self.reservation_store = reservation_store

    def __call__(self) -> WakeSafetyEvidence:
        try:
            active = self.active_jobs.any_active()
            if not active.available:
                return WakeSafetyEvidence(False, False, active.reason or "root state is unavailable")
            if active.value:
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
