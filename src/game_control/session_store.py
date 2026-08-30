"""Durable session and player-count persistence for the slot daemon."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .sessions import SessionEvent, SessionTracker
from . import state_db


class SessionStore:
    def __init__(
        self,
        connection: sqlite3.Connection,
        tracker: SessionTracker | None = None,
        *,
        count_interval_seconds: float = 600.0,
    ) -> None:
        self.connection = connection
        self.tracker = tracker or SessionTracker()
        self.count_interval_seconds = count_interval_seconds
        self._last_counts: dict[str, tuple[int, datetime]] = {}
        self._last_prune: datetime | None = None

    def record(
        self,
        profile_id: str,
        names: set[str] | None,
        count: int | None,
        *,
        now: str,
        source: str = "log",
    ) -> None:
        profile_id = str(profile_id)
        events = self.tracker.observe(profile_id, names, now=now)
        try:
            current_time = _parse_timestamp(now)
        except ValueError:
            current_time = None
        with self.connection:
            self._persist_events(events, source=source)
            if count is not None and self._should_sample(profile_id, count, current_time):
                self.connection.execute(
                    "INSERT INTO metric_samples(profile_id, metric, ts, value) VALUES (?, 'players', ?, ?)",
                    (profile_id, now, float(count)),
                )
                if current_time is not None:
                    self._last_counts[profile_id] = (count, current_time)
            self._maintenance(now, current_time)

    def profile_stopped(self, profile_id: str, *, now: str) -> None:
        events = self.tracker.profile_stopped(str(profile_id), now=now)
        with self.connection:
            self._persist_events(events, source="log")
        self._last_counts.pop(str(profile_id), None)

    def maintain(self, *, now: str) -> None:
        """Run bounded retention maintenance independently of player samples."""
        try:
            current_time = _parse_timestamp(now)
        except ValueError:
            return
        with self.connection:
            self._maintenance(now, current_time)

    def recover(self, *, now: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE player_sessions SET ended_at = ?, source = 'recovered' "
                "WHERE ended_at IS NULL",
                (now,),
            )
        return cursor.rowcount

    def _persist_events(self, events: list[SessionEvent], *, source: str) -> None:
        for event in events:
            if event.kind == "open":
                self.connection.execute(
                    "INSERT INTO player_sessions"
                    "(id, profile_id, player, started_at, ended_at, source)"
                    " VALUES (?, ?, ?, ?, NULL, ?)",
                    (event.session_id, event.profile_id, event.player, event.at, source),
                )
            else:
                self.connection.execute(
                    "UPDATE player_sessions SET ended_at=? WHERE id=? AND ended_at IS NULL",
                    (event.at, event.session_id),
                )

    def _should_sample(
        self, profile_id: str, count: int, current_time: datetime | None
    ) -> bool:
        previous = self._last_counts.get(profile_id)
        if previous is None:
            return True
        previous_count, previous_time = previous
        if previous_count != count:
            return True
        return current_time is not None and (
            current_time - previous_time
        ).total_seconds() >= self.count_interval_seconds

    def _maintenance(self, now: str, current_time: datetime | None) -> None:
        if current_time is None:
            return
        if self._last_prune is not None and (
            current_time - self._last_prune
        ).total_seconds() < 3600:
            return
        state_db.prune_metric_samples(self.connection, now=now)
        state_db.prune_completed_rpc_idempotency(self.connection, now=now)
        self._last_prune = current_time


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


__all__ = ["SessionStore"]
