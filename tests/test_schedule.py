from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from game_control.controller import Controller
from game_control.models import ProfileId
from game_control.protocol import StatusSnapshot
from game_control.schedule import ScheduleBook, parse_schedule


def test_schedule_cron_matches_minute_and_weekday():
    book = ScheduleBook(parse_schedule([{"cron": "0 20 * * 5", "profile": "minecraft"}]))
    friday = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
    assert [item.profile for item in book.due(friday)] == [ProfileId.MINECRAFT]
    assert book.due(friday) == ()
    assert book.due(saturday) == ()


def test_schedule_owner_short_circuit_and_players_guard_skip(tmp_path):
    book = ScheduleBook(parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft"},
        {"cron": "* * * * *", "profile": "pz-rising"},
    ]))
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {
        ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT),
        ProfileId.PZ_RISING: SimpleNamespace(id=ProfileId.PZ_RISING),
    }
    controller._schedule = book
    calls = []
    async def prepare(*args):
        calls.append("prepare")
        return SimpleNamespace(confirmation_id="c" * 32)

    async def confirm(*args):
        calls.append("confirm")

    controller._prepare_switch = prepare
    controller._confirm_switch = confirm
    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc),
        profiles=(
            {"profile_id": "minecraft", "state": "running", "health": "healthy", "slot_owner": "minecraft", "active_job_id": None, "pid": 1, "started_at": None, "uptime_seconds": 1, "cpu_percent": 1, "rss_bytes": 1, "players_online": 0, "installed_version": None, "restart_required": False, "required_ports_ready": True},
            {"profile_id": "pz-rising", "state": "stopped", "health": "unknown", "slot_owner": "minecraft", "active_job_id": None, "pid": None, "started_at": None, "uptime_seconds": None, "cpu_percent": None, "rss_bytes": None, "players_online": None, "installed_version": None, "restart_required": False, "required_ports_ready": False},
        ),
    )
    import asyncio
    asyncio.run(controller._apply_schedules(snapshot, uuid4()))
    assert calls == ["prepare", "confirm"]
