"""Log-derived player session tracking for systemd game profiles."""

from __future__ import annotations

import inspect
import re
import time
from typing import Any

from .models import AdapterKind

_TERRARIA_JOIN = re.compile(r"(?:^|\]\s)(?P<name>[^\]\r\n]{1,64}) has joined\.$")
_TERRARIA_LEAVE = re.compile(r"(?:^|\]\s)(?P<name>[^\]\r\n]{1,64}) has left\.$")


class PlayerTracker:
    def __init__(self):
        self._players: dict[str, set[str]] = {}
        self._running: dict[str, bool] = {}
        self._last_scan: dict[str, float] = {}
        self._scan_interval = 15.0
        self._incremental: set[str] = set()

    def register_incremental(self, profile_id: str) -> None:
        self._incremental.add(str(profile_id))

    async def count(self, profile: Any, adapter: Any, *, running: bool) -> int | None:
        profile_id = str(getattr(getattr(profile, "id", None), "value", getattr(profile, "id", "")))
        adapter_kind = getattr(getattr(profile, "adapter", None), "value", getattr(profile, "adapter", None))
        if adapter_kind != AdapterKind.SYSTEMD.value or profile_id not in {
            "terraria-tmod", "terraria-vanilla"
        }:
            return None
        if self._running.get(profile_id) != running:
            self._players[profile_id] = set()
            self._running[profile_id] = running
            self._last_scan.pop(profile_id, None)
        if not running:
            return 0
        if profile_id in self._incremental:
            return len(self._players.setdefault(profile_id, set()))
        now = time.monotonic()
        if profile_id in self._last_scan and now - self._last_scan[profile_id] < self._scan_interval:
            return len(self._players.setdefault(profile_id, set()))
        if adapter is None or not hasattr(adapter, "recent_logs"):
            return None
        try:
            value = adapter.recent_logs(profile, 5000)
            lines = await value if inspect.isawaitable(value) else value
        except Exception:
            return None
        self._last_scan[profile_id] = now
        players = self._players.setdefault(profile_id, set())
        for line in lines if isinstance(lines, (list, tuple)) else ():
            message = str(getattr(line, "message", ""))
            joined = _TERRARIA_JOIN.search(message)
            left = _TERRARIA_LEAVE.search(message)
            if joined:
                players.add(joined.group("name").strip())
            elif left:
                players.discard(left.group("name").strip())
        return len(players)

    def reset(self, profile_id: str) -> None:
        self._players.pop(profile_id, None)
        self._running.pop(profile_id, None)
        self._last_scan.pop(profile_id, None)

    def names(self, profile_id: str) -> set[str] | None:
        if profile_id not in self._players:
            return None
        return set(self._players[profile_id])

    def ingest_event(self, profile_id: str, event: Any) -> None:
        """Apply one incremental follower event without replaying old logs."""
        if getattr(event, "kind", None) == "reset":
            self.reset(profile_id)
            return
        line = str(getattr(event, "line", ""))
        joined = _TERRARIA_JOIN.search(line)
        left = _TERRARIA_LEAVE.search(line)
        players = self._players.setdefault(profile_id, set())
        if joined:
            players.add(joined.group("name").strip())
        elif left:
            players.discard(left.group("name").strip())


__all__ = ["PlayerTracker"]
