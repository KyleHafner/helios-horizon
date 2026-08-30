from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.controller import Controller
from game_control.idle_stop import IdleStopTracker
from game_control.models import AdapterKind, OperationName, ProfileId
from game_control.protocol import (
    GetStatus,
    RpcRequest,
    RpcSuccess,
    SetIdleStop,
    StatusSnapshot,
)
from game_control.web_main import create_app


def _profile(minutes: int = 5):
    return SimpleNamespace(
        id=ProfileId.MINECRAFT,
        display_name="Minecraft",
        adapter=AdapterKind.SYSTEMD,
        idle_stop_minutes=minutes,
        operations=frozenset({OperationName.STOP}),
        stop_timeout_seconds=5,
    )


def _status(*, players: int | None, state: str = "running", job: str | None = None):
    return SimpleNamespace(
        profile_id=ProfileId.MINECRAFT,
        state=state,
        players_online=players,
        active_job_id=job,
    )


def test_idle_streak_resets_on_unknown_or_nonzero_players():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    tracker = IdleStopTracker(clock=lambda: now)
    profile = _profile(5)

    assert not tracker.observe(profile, _status(players=0))
    now += timedelta(minutes=4)
    assert not tracker.observe(profile, _status(players=None))
    now += timedelta(minutes=5)
    assert not tracker.observe(profile, _status(players=0))
    now += timedelta(minutes=5)
    assert not tracker.observe(profile, _status(players=2))
    now += timedelta(minutes=5)
    assert not tracker.observe(profile, _status(players=0))


def test_idle_streak_fires_once_at_threshold_and_ignores_transitions_and_jobs():
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    tracker = IdleStopTracker(clock=lambda: now)
    profile = _profile(5)

    assert not tracker.observe(profile, _status(players=0, state="starting"))
    assert not tracker.observe(profile, _status(players=0, job="job-1"))
    assert not tracker.observe(profile, _status(players=0))
    now += timedelta(minutes=5)
    assert tracker.observe(profile, _status(players=0))
    assert not tracker.observe(profile, _status(players=0))


def test_idle_stop_controller_uses_normal_stop_actor_and_event(tmp_path):
    profile = _profile(5)
    now = [datetime(2026, 7, 15, tzinfo=timezone.utc)]

    class Adapter:
        def __init__(self):
            self.stops = 0

        async def graceful_stop(self, _profile):
            self.stops += 1

    adapter = Adapter()
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {profile.id: profile}
    controller.adapters = {profile.id: adapter}
    controller._clock = lambda: now[0]
    controller._idle_stop.clock = controller._clock

    class Status:
        async def snapshot(self, *_args):
            return StatusSnapshot(
                generation=1,
                observed_at=now[0],
                profiles=(
                    {
                        "profile_id": "minecraft",
                        "state": "running",
                        "health": "healthy",
                        "slot_owner": "minecraft",
                        "active_job_id": None,
                        "pid": 1,
                        "started_at": now[0],
                        "uptime_seconds": 0,
                        "cpu_percent": 0,
                        "rss_bytes": 1,
                        "players_online": 0,
                        "installed_version": None,
                        "restart_required": False,
                        "required_ports_ready": True,
                    },
                ),
            )

        async def cached_snapshot(self, *_args):
            return await self.snapshot()

    controller.services = SimpleNamespace(status=Status())
    request = lambda: RpcRequest(
        request_id=uuid4(), actor="operator", action=GetStatus(kind="get_status", refresh=True)
    )

    asyncio.run(controller.maintenance_tick())
    now[0] += timedelta(minutes=5)
    asyncio.run(controller.maintenance_tick())
    asyncio.run(controller.maintenance_tick())

    assert adapter.stops == 1
    audit = controller._db().execute(
        "SELECT actor,action,result FROM audit WHERE action='stop'"
    ).fetchall()
    assert audit == [
        ("system:idle-stop", "stop", "accepted"),
        ("system:idle-stop", "stop", "succeeded"),
    ]
    assert controller._db().execute("SELECT code FROM events WHERE code='idle_stop'").fetchall()


def test_idle_stop_rpc_range_route_and_ui_contract():
    with pytest.raises(ValueError):
        SetIdleStop(kind="set_idle_stop", profile_id=ProfileId.MINECRAFT, minutes=4)
    assert SetIdleStop(kind="set_idle_stop", profile_id=ProfileId.MINECRAFT, minutes=0).minutes == 0
    assert SetIdleStop(kind="set_idle_stop", profile_id=ProfileId.MINECRAFT, minutes=30).minutes == 30

    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(request_id=uuid4(), result={"idle_stop_minutes": action.minutes})

    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    headers = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
    csrf = client.get("/api/v1/session", headers=headers).json()["csrf_token"]
    response = client.patch(
        "/api/v1/profiles/minecraft/idle-stop",
        headers={**headers, "X-CSRF-Token": csrf, "Origin": "https://games.example.com"},
        json={"minutes": 30},
    )
    assert response.status_code == 200
    assert isinstance(calls[0][1], SetIdleStop)
    assert calls[0][1].minutes == 30

    web_root = Path(__file__).parents[1] / "web"
    assert "idle-stop-enabled" in (web_root / "index.html").read_text()
    assert "SetIdleStop" in (web_root / "app.js").read_text()
