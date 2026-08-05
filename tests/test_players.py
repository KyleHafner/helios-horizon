from types import SimpleNamespace

import pytest

from game_control.players import PlayerTracker


def _profile(profile_id="terraria-tmod", adapter="systemd"):
    return SimpleNamespace(id=profile_id, adapter=adapter)


@pytest.mark.asyncio
async def test_join_leave_sequence_and_duplicate_join_count_once():
    class Adapter:
        async def recent_logs(self, _profile, _limit):
            return [
                SimpleNamespace(message="Terraria: [x] Kyle has joined."),
                SimpleNamespace(message="Terraria: [x] Kyle has joined."),
                SimpleNamespace(message="Terraria: [x] Meg has joined."),
                SimpleNamespace(message="Terraria: [x] Kyle has left."),
            ]

    assert await PlayerTracker().count(_profile(), Adapter(), running=True) == 1


@pytest.mark.asyncio
async def test_restart_transition_resets_players():
    class Adapter:
        async def recent_logs(self, _profile, _limit):
            return [SimpleNamespace(message="Kyle has joined.")]

    tracker = PlayerTracker()
    adapter = Adapter()
    assert await tracker.count(_profile(), adapter, running=True) == 1
    assert await tracker.count(_profile(), adapter, running=False) == 0
    assert await tracker.count(_profile(), adapter, running=True) == 1


@pytest.mark.asyncio
async def test_unknown_profile_returns_none():
    assert await PlayerTracker().count(_profile("minecraft", "crafty"), None, running=True) is None


@pytest.mark.asyncio
async def test_running_count_throttles_journal_scans(monkeypatch):
    class Adapter:
        def __init__(self):
            self.calls = 0

        async def recent_logs(self, _profile, _limit):
            self.calls += 1
            return [SimpleNamespace(message="Kyle has joined.")]

    now = 100.0
    monkeypatch.setattr("game_control.players.time.monotonic", lambda: now)
    tracker = PlayerTracker()
    adapter = Adapter()
    assert await tracker.count(_profile(), adapter, running=True) == 1
    now += 14.9
    assert await tracker.count(_profile(), adapter, running=True) == 1
    assert adapter.calls == 1
    now += 0.1
    assert await tracker.count(_profile(), adapter, running=True) == 1
    assert adapter.calls == 2
