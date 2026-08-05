"""In-memory player session diff engine; persistence is wired in slotd."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4


@dataclass(frozen=True)
class SessionEvent:
    kind: Literal["open", "close"]
    session_id: str
    profile_id: str
    player: str
    at: str


class SessionTracker:
    def __init__(self) -> None:
        self._open: dict[str, dict[str, str]] = {}

    def observe(
        self, profile_id: str, names: set[str] | None, *, now: str
    ) -> list[SessionEvent]:
        if names is None:
            return []
        current = self._open.setdefault(profile_id, {})
        events: list[SessionEvent] = []
        for player in sorted(names - current.keys()):
            session_id = uuid4().hex
            current[player] = session_id
            events.append(SessionEvent("open", session_id, profile_id, player, now))
        for player in sorted(current.keys() - names):
            events.append(SessionEvent("close", current.pop(player), profile_id, player, now))
        return events

    def profile_stopped(self, profile_id: str, *, now: str) -> list[SessionEvent]:
        current = self._open.pop(profile_id, {})
        return [
            SessionEvent("close", session_id, profile_id, player, now)
            for player, session_id in sorted(current.items())
        ]


__all__ = ["SessionEvent", "SessionTracker"]
