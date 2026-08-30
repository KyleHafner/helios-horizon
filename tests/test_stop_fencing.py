from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from game_control.controller import Controller, _ControllerFailure
from game_control.models import AdapterKind, HealthState, ObservedState, OperationName, ProfileId
from game_control.protocol import (
    ErrorCode,
    ProfileStatus,
    StatusSnapshot,
    Stop,
)


PROFILE = ProfileId.MINECRAFT


def _profile(*, idle_minutes: int = 5):
    return SimpleNamespace(
        id=PROFILE,
        display_name="Minecraft",
        adapter=AdapterKind.SYSTEMD,
        idle_stop_minutes=idle_minutes,
        operations=frozenset({OperationName.START, OperationName.STOP, OperationName.RESTART, OperationName.FORCE_STOP}),
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
    )


def _status(*, players: int | None = 0, job: str | None = None, listener: bool = True):
    return ProfileStatus(
        profile_id=PROFILE,
        state=ObservedState.RUNNING,
        health=HealthState.HEALTHY,
        slot_owner=PROFILE,
        active_job_id=job,
        pid=123,
        started_at="2026-08-06T12:00:00Z",
        uptime_seconds=60,
        cpu_percent=1.0,
        rss_bytes=10,
        players_online=players,
        installed_version="pinned",
        restart_required=False,
        required_ports_ready=listener,
    )


class _Reservations:
    def __init__(self):
        self.current = None

    def reserve_if_available(self, profile, operation_id, _ttl, **_kwargs):
        if self.current is not None and self.current[1] != operation_id:
            raise BlockingIOError("reservation belongs to another operation")
        self.current = (profile, operation_id, 0)

    def renew_if_owned(self, profile, operation_id, **_kwargs):
        return self.current is not None and self.current[:2] == (profile, operation_id)

    def release_if_owned(self, profile, operation_id, generation=0):
        if self.current == (profile, operation_id, generation):
            self.current = None
            return True
        return False


class _Lock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Status:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def snapshot(self, *_args):
        self.calls += 1
        return StatusSnapshot(
            generation=1,
            observed_at="2026-08-06T12:00:00Z",
            profiles=(self.status,),
        )


class _Slot:
    def observe(self):
        return SimpleNamespace(owner=PROFILE.value, inconsistent=False)


class _Adapter:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.graceful_calls = 0
        self.force_calls = 0

    async def graceful_stop(self, _profile):
        self.graceful_calls += 1
        self.started.set()
        await self.release.wait()

    async def force_stop(self, _profile):
        self.force_calls += 1


def _controller(tmp_path, *, status, adapter, reservations):
    profile = _profile()
    controller = Controller(
        profiles={PROFILE: profile},
        reservation_store=reservations,
        adapters={PROFILE: adapter},
        services=SimpleNamespace(status=_Status(status)),
        slot_inspector=_Slot(),
        operation_lock_factory=_Lock,
    )
    return controller


def test_stop_holds_lease_against_join_until_adapter_stop_completes(tmp_path):
    async def scenario():
        reservations = _Reservations()
        adapter = _Adapter()
        controller = _controller(tmp_path, status=_status(), adapter=adapter, reservations=reservations)
        stop_task = asyncio.create_task(controller._stop(Stop(kind="stop", profile_id=PROFILE), "operator", uuid4()))
        await adapter.started.wait()

        with pytest.raises(_ControllerFailure) as conflict:
            await controller._reserve(controller._profile(PROFILE), "start", "join", actor="lazymc")
        assert conflict.value.code is ErrorCode.SLOT_CONFLICT
        assert adapter.graceful_calls == 1

        adapter.release.set()
        result = await stop_task
        assert result.state == "running"
        assert reservations.current is None

    asyncio.run(scenario())


def test_stop_fresh_preflight_rejects_players_job_or_listener_change(tmp_path):
    async def scenario():
        for status in (_status(players=1), _status(job="join-job"), _status(listener=False)):
            reservations = _Reservations()
            adapter = _Adapter()
            controller = _controller(tmp_path, status=status, adapter=adapter, reservations=reservations)
            with pytest.raises(_ControllerFailure) as failure:
                await controller._stop(Stop(kind="stop", profile_id=PROFILE), "operator", uuid4())
            assert failure.value.code is ErrorCode.SLOT_CONFLICT
            assert adapter.graceful_calls == 0
            assert controller.services.status.calls == 1
            assert reservations.current is None

    asyncio.run(scenario())


def test_idle_stop_fence_wins_when_join_arrives_after_idle_sample(tmp_path):
    async def scenario():
        now = [datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)]
        reservations = _Reservations()
        adapter = _Adapter()
        controller = _controller(tmp_path, status=_status(), adapter=adapter, reservations=reservations)
        controller._clock = lambda: now[0]
        controller._idle_stop.clock = controller._clock
        status = _status()
        controller._idle_stop.observe(controller._profile(PROFILE), status)
        now[0] += timedelta(minutes=5)

        async def join_before_stop(*_args):
            await controller._reserve(controller._profile(PROFILE), "start", "join", actor="lazymc")

        controller.services.notifications = SimpleNamespace(send=join_before_stop)
        snapshot = StatusSnapshot(generation=1, observed_at=now[0], profiles=(status,))
        await controller._apply_idle_stops(snapshot, uuid4())

        assert adapter.graceful_calls == 0
        assert reservations.current == (PROFILE, "join", 0)

    asyncio.run(scenario())


def test_force_stop_uses_same_fresh_lease_fence(tmp_path):
    async def scenario():
        reservations = _Reservations()
        adapter = _Adapter()
        controller = _controller(tmp_path, status=_status(players=2), adapter=adapter, reservations=reservations)
        result = await controller._stop_with_fence(
            controller._profile(PROFILE),
            "operator",
            uuid4(),
            force=True,
            allow_players=True,
        )
        assert result.state == "running"
        assert adapter.force_calls == 1
        assert controller.services.status.calls == 1
        assert reservations.current is None

    asyncio.run(scenario())


def test_force_stop_allows_unobservable_players_and_unready_listener(tmp_path):
    async def scenario():
        reservations = _Reservations()
        adapter = _Adapter()
        controller = _controller(
            tmp_path,
            status=_status(players=None, listener=False),
            adapter=adapter,
            reservations=reservations,
        )
        result = await controller._stop_with_fence(
            controller._profile(PROFILE),
            "operator",
            uuid4(),
            force=True,
            allow_players=True,
        )
        assert result.state == "running"
        assert adapter.force_calls == 1
        assert reservations.current is None

    asyncio.run(scenario())


def test_graceful_stop_still_rejects_unobservable_players(tmp_path):
    async def scenario():
        reservations = _Reservations()
        adapter = _Adapter()
        controller = _controller(
            tmp_path,
            status=_status(players=None),
            adapter=adapter,
            reservations=reservations,
        )
        with pytest.raises(_ControllerFailure, match="active players"):
            await controller._stop(Stop(kind="stop", profile_id=PROFILE), "operator", uuid4())
        assert adapter.graceful_calls == 0
        assert reservations.current is None

    asyncio.run(scenario())
