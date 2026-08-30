from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import logging

import pytest

from game_control.controller import Controller
from game_control.models import ProfileId
from game_control.protocol import ErrorCode, GetSchedules, SetSchedules, StatusSnapshot, RpcRequest
from game_control.schedule import ScheduleBook, parse_schedule
from game_control.errors import SafeError


def test_disabled_schedule_is_backward_compatible_and_never_due():
    entries = parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft"},
        {"cron": "* * * * *", "profile": "pz-rising", "enabled": False},
    ])
    assert entries[0].enabled is True
    assert entries[1].enabled is False
    now = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    assert entries[1].next_fire(now) is None
    assert [item.profile for item in ScheduleBook(entries).due(now)] == [ProfileId.MINECRAFT]


def test_daily_backup_schedule_is_typed_and_does_not_become_a_switch():
    entries = parse_schedule([
        {"cron": "0 3 * * *", "profile": "minecraft-sunlit-cobblemon", "backup_destination": "horizon-b2"},
    ])
    assert entries[0].backup_destination.value == "horizon-b2"
    assert entries[0].next_fire(datetime(2026, 7, 10, 2, 59, tzinfo=timezone.utc)).hour == 3


def test_horizon_b2_schedule_requests_protected_backup(tmp_path):
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)}
    controller._schedule = ScheduleBook(parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
    ]))
    captured = []

    async def create(action, actor=None, request_id=None):
        captured.append((action, actor, request_id))
        return SimpleNamespace(job_id="backup", state="running")

    controller.services = SimpleNamespace(backups=SimpleNamespace(create=create))
    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc),
        profiles=(),
    )

    import asyncio
    asyncio.run(controller._apply_schedules(snapshot, uuid4()))

    assert len(captured) == 1
    action = captured[0][0]
    assert action.protected is True
    assert action.destination.value == "horizon-b2"


def test_schedule_fire_marker_prevents_duplicate_after_controller_restart(tmp_path):
    entries = parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
    ])
    first = Controller.for_testing(tmp_path)
    first.profiles = {ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)}
    first._schedule = ScheduleBook(entries)
    calls = []

    async def create(*_args, **_kwargs):
        calls.append("backup")
        return SimpleNamespace(job_id="backup", state="running")

    first.services = SimpleNamespace(backups=SimpleNamespace(create=create))
    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc),
        profiles=(),
    )
    import asyncio
    asyncio.run(first._apply_schedules(snapshot, uuid4()))

    restarted = Controller(
        profiles={ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)},
        state_db=first.state_db,
        operation_lock_factory=lambda: first._operation_lock_factory(),
        schedules=entries,
    )
    restarted.services = SimpleNamespace(backups=SimpleNamespace(create=create))
    asyncio.run(restarted._apply_schedules(snapshot, uuid4()))
    assert calls == ["backup"]


def test_schedule_fire_key_distinguishes_operation_and_backup_destination():
    switch, local_backup, b2_backup = parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft"},
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "local"},
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
    ])
    now = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    keys = {Controller._schedule_fire_key(entry, now) for entry in (switch, local_backup, b2_backup)}
    assert len(keys) == 3
    assert any(":switch:none:" in key for key in keys)
    assert any(":backup:local:" in key for key in keys)
    assert any(":backup:horizon-b2:" in key for key in keys)


def test_duplicate_due_backup_entries_execute_once(tmp_path):
    entries = parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
    ])
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)}
    controller._schedule = ScheduleBook(entries)
    calls = []

    async def create(*_args, **_kwargs):
        calls.append("backup")
        return SimpleNamespace(job_id="backup", state="running")

    controller.services = SimpleNamespace(backups=SimpleNamespace(create=create))
    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc),
        profiles=(),
    )
    import asyncio
    asyncio.run(controller._apply_schedules(snapshot, uuid4()))
    assert calls == ["backup"]


def test_running_scheduled_backup_is_deferred_durable_and_never_stops(caplog, tmp_path):
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)}
    controller._schedule = ScheduleBook(parse_schedule([
        {"cron": "* * * * *", "profile": "minecraft", "backup_destination": "horizon-b2"},
    ]))
    calls = []

    async def create(*_args, **_kwargs):
        calls.append("backup")
        raise SafeError("profile_running", "profile is running; secret=must-not-log")

    controller.services = SimpleNamespace(backups=SimpleNamespace(create=create))
    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc),
        profiles=({
            "profile_id": "minecraft", "state": "running", "health": "healthy",
            "slot_owner": "minecraft", "active_job_id": None, "pid": 42,
            "started_at": None, "uptime_seconds": 1, "cpu_percent": 1,
            "rss_bytes": 1, "players_online": 0, "installed_version": None,
            "restart_required": False, "required_ports_ready": True,
        },),
    )
    with caplog.at_level(logging.WARNING, logger="game_control.controller"):
        import asyncio
        asyncio.run(controller._apply_schedules(snapshot, uuid4()))

    row = controller._db().execute(
        "SELECT operation,state,detail FROM jobs WHERE operation='scheduled_backup'"
    ).fetchone()
    assert calls == ["backup"]
    assert row == ("scheduled_backup", "deferred", "profile_running")
    assert "secret=must-not-log" not in caplog.text
    assert "Traceback" not in caplog.text

    next_snapshot = snapshot.model_copy(update={
        "observed_at": datetime(2026, 7, 17, 20, 1, tzinfo=timezone.utc),
    })
    import asyncio
    asyncio.run(controller._apply_schedules(next_snapshot, uuid4()))
    assert len(controller._db().execute(
        "SELECT id FROM jobs WHERE operation='scheduled_backup'"
    ).fetchall()) == 2


def test_schedule_rejects_non_boolean_enabled():
    with pytest.raises(ValueError, match="invalid schedule enabled"):
        parse_schedule([{"cron": "* * * * *", "profile": "minecraft", "enabled": "false"}])


def test_schedule_cron_matches_minute_and_weekday():
    book = ScheduleBook(parse_schedule([{"cron": "0 20 * * 5", "profile": "minecraft"}]))
    friday = datetime(2026, 7, 17, 20, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 7, 18, 20, 0, tzinfo=timezone.utc)
    assert [item.profile for item in book.due(friday)] == [ProfileId.MINECRAFT]
    assert book.due(friday) == ()
    assert book.due(saturday) == ()


def test_next_fire_skips_an_already_elapsed_current_minute():
    entry = parse_schedule([{"cron": "0 * * * *", "profile": "minecraft"}])[0]
    now = datetime(2026, 7, 17, 20, 0, 30, tzinfo=timezone.utc)
    assert entry.next_fire(now) == datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc)


def test_next_fire_accepts_leap_day_within_four_year_horizon():
    entry = parse_schedule([{"cron": "0 0 29 2 *", "profile": "minecraft"}])[0]
    now = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert entry.next_fire(now) == datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc)


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


def test_schedule_rpc_reloads_book_for_the_next_minute(tmp_path):
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {
        ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT),
        ProfileId.PZ_RISING: SimpleNamespace(id=ProfileId.PZ_RISING),
    }
    controller.schedule_config_path = tmp_path / "game-control.toml"
    controller.schedule_config_path.write_text("profiles_dir = \"profiles.d\"\n", encoding="utf-8")
    calls = []

    async def prepare(*args):
        calls.append("prepare")
        return SimpleNamespace(confirmation_id="c" * 32)

    async def confirm(*args):
        calls.append("confirm")

    controller._prepare_switch = prepare
    controller._confirm_switch = confirm
    response = controller.execute_sync(RpcRequest(
        request_id=uuid4(),
        actor="operator",
        action=SetSchedules(kind="set_schedules", entries=(
            {"cron": "1 * * * *", "profile": "pz-rising", "enabled": False},
        )),
    ))
    assert response.result.schedules[0].profile == ProfileId.PZ_RISING
    listed = controller.execute_sync(RpcRequest(
        request_id=uuid4(), actor="operator", action=GetSchedules(kind="get_schedules"),
    ))
    assert listed.ok is True
    assert listed.result.schedules[0].cron == "1 * * * *"
    assert listed.result.schedules[0].enabled is False
    assert listed.result.schedules[0].next_fire is None

    snapshot = StatusSnapshot(
        generation=1,
        observed_at=datetime(2026, 7, 17, 20, 1, tzinfo=timezone.utc),
        profiles=(
            {"profile_id": "minecraft", "state": "running", "health": "healthy", "slot_owner": "minecraft", "active_job_id": None, "pid": 1, "started_at": None, "uptime_seconds": 1, "cpu_percent": 1, "rss_bytes": 1, "players_online": 0, "installed_version": None, "restart_required": False, "required_ports_ready": True},
            {"profile_id": "pz-rising", "state": "stopped", "health": "unknown", "slot_owner": "minecraft", "active_job_id": None, "pid": None, "started_at": None, "uptime_seconds": None, "cpu_percent": None, "rss_bytes": None, "players_online": None, "installed_version": None, "restart_required": False, "required_ports_ready": False},
        ),
    )
    import asyncio
    asyncio.run(controller._apply_schedules(snapshot, uuid4()))
    assert calls == []


def test_schedule_rpc_rejects_invalid_cron_and_unknown_profile(tmp_path):
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {ProfileId.MINECRAFT: SimpleNamespace(id=ProfileId.MINECRAFT)}
    controller.schedule_config_path = tmp_path / "game-control.toml"
    controller.schedule_config_path.write_text('profiles_dir = "profiles.d"\n', encoding="utf-8")
    invalid_cron = controller.execute_sync(RpcRequest(
        request_id=uuid4(), actor="operator",
        action=SetSchedules(kind="set_schedules", entries=({"cron": "0 0 31 2 *", "profile": "minecraft"},)),
    ))
    assert invalid_cron.error.code == ErrorCode.INVALID_REQUEST
    assert "no fire time within the next four years" in invalid_cron.error.message
    unknown_profile = controller.execute_sync(RpcRequest(
        request_id=uuid4(), actor="operator",
        action=SetSchedules(kind="set_schedules", entries=({"cron": "* * * * *", "profile": "pz-rising"},)),
    ))
    assert unknown_profile.error.code == ErrorCode.INVALID_REQUEST
    assert list(tmp_path.glob("*.bak")) == []
