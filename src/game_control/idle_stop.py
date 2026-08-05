"""Fail-open idle streak accounting for controller-owned automatic stops."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


@dataclass
class _Streak:
    started_at: datetime
    attempted: bool = False


class IdleStopTracker:
    """Track one zero-player streak per profile without owning a timer."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._streaks: dict[str, _Streak] = {}

    def observe(self, profile: Any, status: Any) -> bool:
        """Return whether this fresh status sample crosses the stop threshold."""

        minutes = getattr(profile, "idle_stop_minutes", 0) or 0
        key = self._key(getattr(profile, "id", None))
        if not isinstance(minutes, int) or minutes <= 0:
            self._streaks.pop(key, None)
            return False

        state = self._value(getattr(status, "state", None))
        players = getattr(status, "players_online", None)
        job = getattr(status, "active_job_id", None)
        if state != "running" or players is None or players != 0 or job is not None:
            self._streaks.pop(key, None)
            return False

        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        streak = self._streaks.get(key)
        if streak is None:
            self._streaks[key] = _Streak(started_at=now)
            return False
        if streak.attempted:
            return False
        if now - streak.started_at < timedelta(minutes=minutes):
            return False
        streak.attempted = True
        return True

    def reset(self, profile_id: Any) -> None:
        self._streaks.pop(self._key(profile_id), None)

    @staticmethod
    def _key(value: Any) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)


__all__ = ["IdleStopTracker"]
