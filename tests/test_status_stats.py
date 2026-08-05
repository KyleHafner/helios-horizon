from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from game_control.adapters.base import AdapterObservation
from game_control.status import StatusService


@pytest.mark.asyncio
async def test_status_records_running_names_and_closes_on_stop():
    profile = SimpleNamespace(id="terraria-tmod", adapter="systemd", ports=())

    class Adapter:
        def __init__(self):
            self.running = True

        async def observe(self, _profile):
            return AdapterObservation(
                running=self.running,
                players_online=1 if self.running else 0,
                player_names=("PlayerOne",) if self.running else (),
            )

    class Store:
        def __init__(self):
            self.records = []
            self.stops = []

        def record(self, *args, **kwargs):
            self.records.append((args, kwargs))

        def profile_stopped(self, *args, **kwargs):
            self.stops.append((args, kwargs))

    adapter = Adapter()
    store = Store()
    now = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    service = StatusService(
        [profile],
        adapter=adapter,
        session_store=store,
        clock=lambda: now,
    )

    await service.snapshot()
    adapter.running = False
    await service.snapshot()

    assert store.records[0][0] == ("terraria-tmod", {"PlayerOne"}, 1)
    assert store.records[0][1]["source"] == "log"
    assert store.stops[0][0] == ("terraria-tmod",)
