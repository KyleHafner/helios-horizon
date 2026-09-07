from uuid import uuid4
from pathlib import Path
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from game_control.controller import (
    ACTION_CLASSES,
    Controller,
    STREAM_ACTIONS,
    _ActionClass,
    _ControllerFailure,
    _action_class,
    dispatch_is_exhaustive,
)
from game_control.errors import SafeError
from game_control.models import (
    AdapterKind,
    OperationName,
    PathSpec,
    PortSpec,
    ProcessSpec,
    Profile,
    ProfileId,
    UpdateSpec,
)
from game_control.protocol import (
    ConfirmForceStop,
    CreateBackup,
    ConfirmSwitch,
    ErrorCode,
    Watch,
    GetStatus,
    JobAccepted,
    PrepareForceStop,
    PrepareSwitch,
    RpcRequest,
    RunBenchmark,
    StatusSnapshot,
    Start,
    Stop,
    SwitchOptions,
    WaitReadiness,
)
from game_control.health import ReadinessOutcome
from game_control.slot import OperationLock, ReservationStore


@pytest.mark.asyncio
async def test_status_reports_initializing_until_startup_reconcile_finishes(tmp_path):
    controller = Controller.for_testing(tmp_path)
    request = lambda: RpcRequest(
        request_id=uuid4(),
        actor="operator",
        action=GetStatus(kind="get_status"),
    )

    before = await controller.execute(request())
    assert before.ok and before.result.initializing is True

    await controller.reconcile_startup()

    after = await controller.execute(request())
    assert after.ok and after.result.initializing is False


@pytest.mark.asyncio
async def test_wait_readiness_returns_generation_bound_internal_outcome(tmp_path):
    controller = Controller.for_testing(tmp_path)
    ticket = controller._readiness.begin(ProfileId.MINECRAFT_SUNLIT_COBBLEMON)
    assert controller._readiness.notify(ticket, ReadinessOutcome.SUCCESS)

    response = await controller.execute(RpcRequest(
        request_id=uuid4(),
        actor="capability-waker",
        action=WaitReadiness(
            kind="wait_readiness",
            profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
            generation=ticket.generation,
            timeout_seconds=1,
        ),
    ))

    assert response.ok
    assert response.result.generation == ticket.generation
    assert response.result.outcome == "success"


@pytest.mark.asyncio
async def test_wait_readiness_refuses_stale_generation(tmp_path):
    controller = Controller.for_testing(tmp_path)
    stale = controller._readiness.begin(ProfileId.MINECRAFT_SUNLIT_COBBLEMON)
    controller._readiness.begin(ProfileId.MINECRAFT_SUNLIT_COBBLEMON)

    response = await controller.execute(RpcRequest(
        request_id=uuid4(),
        actor="capability-waker",
        action=WaitReadiness(
            kind="wait_readiness",
            profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
            generation=stale.generation,
            timeout_seconds=1,
        ),
    ))

    assert not response.ok
    assert response.error.code.value == "health_failed"


@pytest.mark.asyncio
async def test_get_status_uses_cached_projection_unless_refresh_requested(tmp_path):
    class Status:
        def __init__(self):
            self.cached_calls = 0
            self.refresh_calls = 0

        async def cached_snapshot(self, *_args):
            self.cached_calls += 1
            return {"kind": "cached"}

        async def snapshot(self, *_args):
            self.refresh_calls += 1
            return {"kind": "fresh"}

    status = Status()
    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=status)

    cached = await controller.execute(RpcRequest(
        request_id=uuid4(), actor="operator", action=GetStatus(kind="get_status")
    ))
    fresh = await controller.execute(RpcRequest(
        request_id=uuid4(), actor="operator", action=GetStatus(kind="get_status", refresh=True)
    ))

    assert cached.result == {"kind": "cached"}
    assert fresh.result == {"kind": "fresh"}
    assert status.cached_calls == 1
    assert status.refresh_calls == 1


@pytest.mark.asyncio
async def test_get_status_cached_read_stays_responsive_during_slow_start_refresh(tmp_path):
    refresh_entered = asyncio.Event()
    refresh_release = asyncio.Event()

    class Status:
        async def cached_snapshot(self, *_args):
            return {"state": "starting", "health": "unknown", "players_online": None}

        async def snapshot(self, *_args):
            refresh_entered.set()
            await refresh_release.wait()
            return {"state": "running", "health": "healthy", "players_online": 0}

    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=Status())
    refresh = asyncio.create_task(controller.execute(RpcRequest(
        request_id=uuid4(), actor="status-publisher",
        action=GetStatus(kind="get_status", refresh=True),
    )))
    await refresh_entered.wait()

    response = await asyncio.wait_for(controller.execute(RpcRequest(
        request_id=uuid4(), actor="operator",
        action=GetStatus(kind="get_status"),
    )), timeout=0.2)
    assert response.ok
    assert response.result["state"] == "starting"
    assert response.result["players_online"] is None

    refresh_release.set()
    await refresh


class _MemoryLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_dispatch_is_exhaustive() -> None:
    assert dispatch_is_exhaustive()


def test_only_watch_is_handled_outside_controller_and_future_actions_fail(monkeypatch) -> None:
    import game_control.controller as controller_module
    from typing import Union

    assert STREAM_ACTIONS == frozenset({Watch})
    assert Watch not in controller_module.DISPATCH
    class FutureAction:
        pass
    monkeypatch.setattr(controller_module, "RpcAction", Union[Watch, FutureAction])
    assert not dispatch_is_exhaustive()


@pytest.mark.asyncio
async def test_benchmark_is_accepted_as_background_job_and_blocks_profile_start(tmp_path):
    profile = _profile().model_copy(
        update={"operations": frozenset({OperationName.START, OperationName.BENCHMARK})}
    )
    release = asyncio.Event()
    started = asyncio.Event()

    class Benchmarks:
        async def prove_idle(self, _profile_id):
            return None

        def preflight(self, _action, _job_id):
            return {"version": 2, "preflight": {}}

        def prepare_frozen(self, _action, _job_id, _provenance):
            return None

        async def run(self, _action, _job_id):
            started.set()
            await release.wait()

        def fail(self, _job_id, _code):
            raise AssertionError("successful benchmark must not fail")

    controller = Controller.for_testing(tmp_path)
    class Store:
        def __init__(self):
            self.owner = None
            self.releases = []
        def reserve_if_available(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner is not None and self.owner[0:2] != (profile_id, operation_id):
                raise BlockingIOError("owned")
            self.owner = (profile_id, operation_id, kwargs.get("state_generation", 0))
        def renew_if_owned(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner != (profile_id, operation_id, kwargs.get("state_generation", 0)):
                raise BlockingIOError("ownership changed")
            return self.owner
        def release_if_owned(self, *lease):
            self.releases.append(lease)
            if self.owner == lease:
                self.owner = None
                return True
            return False
    store = Store()
    controller.reservation_store = store
    controller.profiles = {profile.id: profile}
    controller.services = SimpleNamespace(benchmarks=Benchmarks())
    action = RunBenchmark(
        kind="run_benchmark",
        profile_id=profile.id,
        baseline_preset="current",
        candidate_preset="candidate",
    )
    response = await controller.execute(
        RpcRequest(request_id=uuid4(), actor="operator", action=action)
    )
    assert response.ok and response.result.state == "accepted"
    await started.wait()
    assert store.owner is not None

    blocked = await controller.execute(
        RpcRequest(
            request_id=uuid4(),
            actor="operator",
            action=Start(kind="start", profile_id=profile.id),
        )
    )
    assert not blocked.ok
    assert blocked.error.code.value == "invalid_state"

    tasks = tuple(controller._background_tasks)
    release.set()
    await asyncio.gather(*tasks)
    state = controller._db().execute(
        "SELECT state FROM jobs WHERE id=?", (response.result.job_id,)
    ).fetchone()[0]
    assert state == "succeeded"
    assert store.owner is None
    assert len(store.releases) == 1


@pytest.mark.asyncio
async def test_benchmark_renewal_loss_drains_preflight_and_releases_real_reservation(tmp_path):
    profile = _profile().model_copy(
        update={"operations": frozenset({OperationName.BENCHMARK})}
    )
    operation = tmp_path / "operation.lock"
    operation.touch(mode=0o600)
    release_observations = []
    preflight_started = threading.Event()
    preflight_done = threading.Event()
    preflight_release = threading.Event()

    class Store(ReservationStore):
        def release_if_owned(self, *lease):
            release_observations.append(preflight_done.is_set())
            return super().release_if_owned(*lease)

    store = Store(operation, tmp_path / "reservation.json")

    class Benchmarks:
        async def prove_idle(self, _profile_id):
            return None

        def preflight(self, _action, _job_id):
            preflight_started.set()
            assert preflight_release.wait(2)
            preflight_done.set()
            return {"version": 2, "preflight": {}}

        def prepare_frozen(self, *_args):
            raise AssertionError("lease loss must prevent benchmark insertion")

        async def run(self, *_args):
            raise AssertionError("lease loss must prevent worker launch")

        def fail(self, *_args):
            raise AssertionError("lease loss before insertion has no benchmark row to fail")

    controller = Controller(
        profiles={profile.id: profile},
        reservation_store=store,
        operation_lock_factory=lambda: OperationLock(operation),
        services=SimpleNamespace(benchmarks=Benchmarks()),
    )
    renewal_failed = asyncio.Event()

    async def failing_renewal():
        await renewal_failed.wait()
        raise RuntimeError("reservation renewal failed")

    controller._lease_renewal = lambda _lease: asyncio.create_task(failing_renewal())
    action = RunBenchmark(
        kind="run_benchmark",
        profile_id=profile.id,
        baseline_preset="current",
        candidate_preset="candidate",
    )
    task = asyncio.create_task(controller._run_benchmark(action, "operator", uuid4()))
    assert await asyncio.to_thread(preflight_started.wait, 1)
    renewal_failed.set()
    preflight_release.set()

    with pytest.raises(_ControllerFailure):
        await asyncio.wait_for(task, timeout=2)
    assert preflight_done.is_set()
    assert release_observations == [True]
    assert store.read() is None
    assert controller._db().execute(
        "SELECT state,detail FROM jobs WHERE operation='benchmark'"
    ).fetchone() == ("failed", "benchmark request validation failed")
    assert controller._db().execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='benchmark_runs'"
    ).fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["backup", "restore", "update", "world_clone"])
async def test_held_maintenance_lease_fences_start_and_is_generation_safe(tmp_path, operation):
    """Every heavy maintenance class shares the durable start reservation."""
    profile = _profile().model_copy(update={
        "operations": frozenset({OperationName.START, OperationName.BACKUP,
                                  OperationName.RESTORE, OperationName.UPDATE_APPLY,
                                  OperationName.CLONE_SOURCE, OperationName.CLONE_TARGET})
    })
    class Store:
        def __init__(self):
            self.owner = None
            self.releases = []
        def reserve_if_available(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner is not None and self.owner[0:2] != (profile_id, operation_id):
                raise BlockingIOError("owned")
            self.owner = (profile_id, operation_id, kwargs.get("state_generation", 0))
        def renew_if_owned(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner != (profile_id, operation_id, kwargs.get("state_generation", 0)):
                raise BlockingIOError("ownership changed")
            return self.owner
        def release_if_owned(self, *lease):
            self.releases.append(lease)
            if self.owner == lease:
                self.owner = None
                return True
            return False
    store = Store()
    controller = Controller(
        profiles={profile.id: profile}, reservation_store=store,
        adapters={profile.id: _Adapter()}, operation_lock_factory=_MemoryLock,
        await_ready=lambda _profile: True, await_free_slot=lambda: True,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    async def held():
        async with controller._operation_lease(profile, operation, uuid4(), actor="test"):
            entered.set()
            await release.wait()
    task = asyncio.create_task(held())
    await entered.wait()
    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())
    assert exc_info.value.code is ErrorCode.SLOT_CONFLICT
    lease = store.owner
    assert lease is not None
    # A different generation cannot release the active owner.
    assert store.release_if_owned(lease[0], lease[1], lease[2] + 1) is False
    assert store.owner == lease
    release.set()
    await task
    assert store.owner is None
    assert store.releases[-1] == lease


@pytest.mark.asyncio
async def test_successful_operation_does_not_hide_reservation_release_failure(tmp_path):
    profile = _profile().model_copy(update={"operations": frozenset({OperationName.BACKUP})})

    class Store:
        def reserve_if_available(self, profile_id, operation_id, ttl, **kwargs):
            self.lease = (profile_id, operation_id, kwargs.get("state_generation", 0))

        def renew_if_owned(self, *_args, **_kwargs):
            return self.lease

    controller = Controller(
        profiles={profile.id: profile},
        reservation_store=Store(),
        operation_lock_factory=_MemoryLock,
    )

    async def failed_release(_lease=None):
        raise RuntimeError("reservation release failed")

    controller._clear_reservation = failed_release
    with pytest.raises(RuntimeError, match="reservation release failed"):
        async with controller._operation_lease(profile, "backup", uuid4(), actor="test"):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["backup", "restore", "update", "world_clone"])
async def test_held_start_fences_each_maintenance_class(tmp_path, operation):
    profile = _profile().model_copy(update={
        "operations": frozenset({OperationName.START, OperationName.BACKUP,
                                  OperationName.RESTORE, OperationName.UPDATE_APPLY,
                                  OperationName.CLONE_SOURCE, OperationName.CLONE_TARGET})
    })
    class Store:
        def __init__(self): self.owner = None
        def reserve_if_available(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner is not None and self.owner[0:2] != (profile_id, operation_id):
                raise BlockingIOError("owned")
            self.owner = (profile_id, operation_id, kwargs.get("state_generation", 0))
        def renew_if_owned(self, profile_id, operation_id, ttl, **kwargs):
            if self.owner != (profile_id, operation_id, kwargs.get("state_generation", 0)):
                raise BlockingIOError("ownership changed")
            return self.owner
        def release_if_owned(self, *lease):
            if self.owner == lease: self.owner = None
            return True
    store = Store()
    controller = Controller(
        profiles={profile.id: profile}, reservation_store=store,
        adapters={profile.id: _Adapter()}, operation_lock_factory=_MemoryLock,
        await_ready=lambda _profile: True, await_free_slot=lambda: True,
    )
    release = asyncio.Event()
    async def held_start():
        lease, renewal = await controller._operation_lease_acquire(profile, "start", uuid4(), actor="test")
        try:
            await release.wait()
        finally:
            await controller._operation_lease_release(lease, renewal)
    task = asyncio.create_task(held_start())
    while store.owner is None:
        await asyncio.sleep(0)
    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._operation_lease_acquire(profile, operation, uuid4(), actor="test")
    assert exc_info.value.code is ErrorCode.SLOT_CONFLICT
    release.set()
    await task


@pytest.mark.asyncio
async def test_backup_uses_durable_job_id_and_replays_without_repeat_work(tmp_path):
    profile = _profile().model_copy(update={"operations": frozenset({OperationName.BACKUP})})
    calls = 0

    class Backups:
        async def create(self, action, actor=None, request_id=None, **_kwargs):
            nonlocal calls
            calls += 1
            return JobAccepted(job_id="facade-id-must-not-escape", state="running")

    controller = Controller(
        profiles={profile.id: profile}, operation_lock_factory=_MemoryLock,
        services=SimpleNamespace(backups=Backups()),
    )
    request = RpcRequest(request_id=uuid4(), actor="test", action=CreateBackup(kind="create_backup", profile_id=profile.id))
    first = await controller.execute(request)
    second = await controller.execute(request)
    assert first.ok and second.ok
    assert first.result.job_id == second.result.job_id
    assert first.result.job_id != "facade-id-must-not-escape"
    assert calls == 1
    assert controller._db().execute(
        "SELECT state FROM jobs WHERE id=?", (first.result.job_id,)
    ).fetchone() == ("succeeded",)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,code", [
    ("restore", ErrorCode.RESTORE_FAILED),
    ("update", ErrorCode.UPDATE_FAILED),
    ("world_clone", ErrorCode.INVALID_REQUEST),
])
async def test_maintenance_rows_are_truthful_and_safe_errors_are_typed(tmp_path, operation, code):
    profile = _profile()
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {profile.id: profile}

    async def service(action, **_kwargs):
        raise SafeError("service_specific_code", "bounded service failure", retryable=True)

    with pytest.raises(_ControllerFailure) as raised:
        await controller._run_maintenance_job(
            profile, operation, SimpleNamespace(), "operator", uuid4(), service
        )
    assert raised.value.code is code
    assert raised.value.retryable is True
    row = controller._db().execute(
        "SELECT id,state,operation,detail FROM jobs WHERE operation=?", (operation,)
    ).fetchone()
    assert row[1:] == ("failed", operation, "bounded service failure")


def test_controller_deduplicates_exact_replay(tmp_path) -> None:
    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: _Adapter()},
        operation_lock_factory=_MemoryLock,
        await_ready=lambda _profile: True,
    )
    request = RpcRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "actor": "swag",
            "action": {"kind": "start", "profile_id": "minecraft"},
        }
    )
    first = controller.execute_sync(request)
    second = controller.execute_sync(request)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert controller._readiness.health()["success"] == 1
    assert controller._readiness.health()["active"] == 0
    assert controller._db().execute("SELECT COUNT(*) FROM rpc_idempotency").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_repeated_pure_read_request_id_is_not_replayed(tmp_path) -> None:
    class Status:
        def __init__(self):
            self.calls = 0

        async def cached_snapshot(self, *_args):
            self.calls += 1
            return {"calls": self.calls}

    status = Status()
    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=status)
    request = RpcRequest(
        request_id=uuid4(),
        actor="operator",
        action=GetStatus(kind="get_status"),
    )

    first = await controller.execute(request)
    second = await controller.execute(request)

    assert first.result == {"calls": 1}
    assert second.result == {"calls": 2}
    assert controller._db().execute("SELECT COUNT(*) FROM rpc_idempotency").fetchone()[0] == 0


def test_every_declared_pure_read_is_classified_outside_replay() -> None:
    pure_reads = [kind for kind, classification in ACTION_CLASSES.items() if classification is _ActionClass.PURE_READ]
    assert pure_reads
    assert all(_action_class(kind.model_construct()) is _ActionClass.PURE_READ for kind in pure_reads if kind is not GetStatus)
    assert _action_class(GetStatus(kind="get_status", refresh=True)) is _ActionClass.PURE_READ


@pytest.mark.asyncio
async def test_refresh_status_request_id_is_not_replayed_after_p021(tmp_path) -> None:
    class Status:
        def __init__(self):
            self.calls = 0

        async def snapshot(self, *_args):
            self.calls += 1
            return {"calls": self.calls}

    status = Status()
    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=status)
    request = RpcRequest(
        request_id=uuid4(),
        actor="operator",
        action=GetStatus(kind="get_status", refresh=True),
    )

    first = await controller.execute(request)
    second = await controller.execute(request)

    assert first.result == {"calls": 1}
    assert second.result == {"calls": 2}
    assert status.calls == 2
    assert controller._db().execute("SELECT COUNT(*) FROM rpc_idempotency").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_maintenance_tick_refreshes_status_and_runs_retention_without_web(tmp_path) -> None:
    calls = []
    maintained = []

    class Status:
        async def snapshot(self, *_args):
            calls.append("snapshot")
            return StatusSnapshot(generation=1, observed_at=datetime.now(timezone.utc), profiles=())

    class SessionStore:
        def maintain(self, *, now):
            maintained.append(now)

    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=Status(), session_store=SessionStore())
    await controller.maintenance_tick()
    assert calls == ["snapshot"]
    assert len(maintained) == 1


@pytest.mark.asyncio
async def test_maintenance_ticks_are_single_flight(tmp_path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0

    class Status:
        async def snapshot(self, *_args):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1
            return StatusSnapshot(generation=1, observed_at=datetime.now(timezone.utc), profiles=())

    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=Status())
    first = asyncio.create_task(controller.maintenance_tick())
    await entered.wait()
    second = asyncio.create_task(controller.maintenance_tick())
    await asyncio.sleep(0)
    assert maximum == 1
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_maintenance_snapshot_typeerror_is_not_retried(tmp_path) -> None:
    calls = 0

    class Status:
        async def snapshot(self, *_args, maintenance=False):
            nonlocal calls
            calls += 1
            raise TypeError("snapshot body failure")

    controller = Controller.for_testing(tmp_path)
    controller.services = SimpleNamespace(status=Status())
    with pytest.raises(TypeError, match="snapshot body failure"):
        await controller.maintenance_tick()
    assert calls == 1


@pytest.mark.asyncio
async def test_transaction_flock_wait_does_not_block_event_loop(tmp_path) -> None:
    release = threading.Event()

    class BlockingLock:
        def __enter__(self):
            release.wait(timeout=1.0)
            return self

        def __exit__(self, *_args):
            return False

    controller = Controller.for_testing(tmp_path)
    controller._operation_lock_factory = BlockingLock
    ticked = False

    async def tick() -> None:
        nonlocal ticked
        await asyncio.sleep(0.02)
        ticked = True

    timer = threading.Timer(0.15, release.set)
    timer.start()
    ticker = asyncio.create_task(tick())
    try:
        assert await controller._transaction(lambda: "ok") == "ok"
        assert ticked is True
        await ticker
    finally:
        timer.cancel()


@pytest.mark.asyncio
async def test_cancelled_transaction_releases_late_flock_acquisition(tmp_path) -> None:
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    class BlockingLock:
        def __enter__(self):
            started.set()
            release.wait(timeout=1.0)
            return self

        def __exit__(self, *_args):
            exited.set()
            return False

    controller = Controller.for_testing(tmp_path)
    controller._operation_lock_factory = BlockingLock
    transaction = asyncio.create_task(controller._transaction(lambda: "unexpected"))
    while not started.is_set():
        await asyncio.sleep(0)

    transaction.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await transaction

    assert exited.is_set()


@pytest.mark.asyncio
async def test_transaction_releases_lock_when_callback_raises(tmp_path) -> None:
    exited = threading.Event()

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            exited.set()
            return False

    controller = Controller.for_testing(tmp_path)
    controller._operation_lock_factory = Lock
    with pytest.raises(RuntimeError, match="callback failed"):
        await controller._transaction(lambda: (_ for _ in ()).throw(RuntimeError("callback failed")))
    assert exited.is_set()


@pytest.mark.asyncio
async def test_cancelled_transaction_waits_for_release_during_exit(tmp_path) -> None:
    exit_started = threading.Event()
    release_exit = threading.Event()
    exited = threading.Event()

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            exit_started.set()
            release_exit.wait(timeout=1.0)
            exited.set()
            return False

    controller = Controller.for_testing(tmp_path)
    controller._operation_lock_factory = Lock
    transaction = asyncio.create_task(controller._transaction(lambda: "ok"))
    while not exit_started.is_set():
        await asyncio.sleep(0)
    transaction.cancel()
    release_exit.set()
    with pytest.raises(asyncio.CancelledError):
        await transaction
    assert exited.is_set()


def _profile() -> Profile:
    return Profile(
        id=ProfileId.MINECRAFT,
        display_name="Minecraft",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="minecraft.service",
        process=ProcessSpec(executable=Path("/usr/bin/java")),
        ports=(PortSpec(protocol="tcp", port=25565),),
        start_timeout_seconds=5,
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
        paths=PathSpec(
            data_roots=(Path("/var/lib/game-control/minecraft"),),
            mutable_root=Path("/var/lib/game-control/minecraft"),
            backup_root=Path("/var/backups/game-control/minecraft"),
            install_root=Path("/opt/game-control/minecraft"),
            version_file=Path("/var/lib/game-control/minecraft/version"),
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.START, OperationName.STOP}),
        update=UpdateSpec(kind="manual"),
    )


class _Adapter:
    def __init__(self):
        self.started = 0
        self.forced = 0

    async def start(self, profile):
        self.started += 1

    async def graceful_stop(self, profile):
        await asyncio.sleep(0)

    async def force_stop(self, profile):
        self.forced += 1


class _RunningAdapter(_Adapter):
    async def observe(self, profile):
        return type("Observation", (), {"running": True})()


class _FailingStopAdapter(_Adapter):
    async def graceful_stop(self, profile):
        raise RuntimeError("stop failed")


def _boot_controller(profile, adapter, state_db=None) -> Controller:
    return Controller(
        profiles={profile.id: profile},
        state_db=state_db,
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
        boot_profile="resume-last-active",
        boot_autostart=True,
    )


def test_request_id_conflict_is_rejected(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    request_id = uuid4()
    first = RpcRequest.model_validate(
        {
            "request_id": str(request_id),
            "actor": "swag",
            "action": {"kind": "start", "profile_id": "minecraft"},
        }
    )
    second = RpcRequest.model_validate(
        {
            "request_id": str(request_id),
            "actor": "swag",
            "action": {"kind": "stop", "profile_id": "minecraft"},
        }
    )
    controller.execute_sync(first)
    response = controller.execute_sync(second)
    assert response.error.code.value == "request_id_conflict"


@pytest.mark.asyncio
async def test_concurrent_same_request_id_runs_one_side_effect(tmp_path) -> None:
    profile = _profile()
    adapter = _Adapter()
    class Lock:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: adapter},
        operation_lock_factory=Lock,
        await_ready=lambda _profile: True,
    )
    request = RpcRequest.model_validate({
        "request_id": str(uuid4()), "actor": "swag",
        "action": {"kind": "start", "profile_id": "minecraft"},
    })
    responses = await asyncio.gather(controller.execute(request), controller.execute(request))
    assert responses[0].model_dump(mode="json") == responses[1].model_dump(mode="json")
    assert adapter.started == 1


@pytest.mark.asyncio
async def test_rejected_concurrent_start_cannot_replace_genuine_readiness_generation(tmp_path) -> None:
    profile = _profile()
    entered_start = asyncio.Event()
    release_start = asyncio.Event()

    class Adapter(_Adapter):
        async def start(self, _profile):
            entered_start.set()
            await release_start.wait()
            self.started += 1

    adapter = Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
    )
    reserve_calls = 0

    async def reserve(*_args, **_kwargs):
        nonlocal reserve_calls
        reserve_calls += 1
        if reserve_calls > 1:
            raise _ControllerFailure(ErrorCode.SLOT_CONFLICT, "slot is occupied")
        return (profile.id, "genuine", 1)

    controller._reserve = reserve
    controller._lease_renewal = lambda _lease: asyncio.create_task(asyncio.sleep(3_600))
    controller._clear_reservation = lambda _lease=None: asyncio.sleep(0)

    genuine = asyncio.create_task(controller._start(
        Start(kind="start", profile_id=profile.id), "owner", uuid4()
    ))
    await entered_start.wait()
    ticket = controller._readiness.latest(profile.id)

    with pytest.raises(_ControllerFailure, match="slot is occupied"):
        await controller._start(
            Start(kind="start", profile_id=profile.id), "contender", uuid4()
        )
    assert controller._readiness.latest(profile.id) == ticket

    release_start.set()
    result = await genuine
    assert result.readiness_generation == ticket.generation
    assert result.readiness == "success"
    assert await controller._readiness.wait(ticket, timeout=1) is ReadinessOutcome.SUCCESS


@pytest.mark.asyncio
async def test_confirmation_rejects_intervening_generation(tmp_path) -> None:
    profile = _profile()
    profile = profile.model_copy(update={"operations": frozenset({OperationName.FORCE_STOP, OperationName.START, OperationName.STOP})})
    class Lock:
        def __enter__(self): return self
        def __exit__(self, *_): return False
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: _Adapter()},
        operation_lock_factory=Lock,
    )
    prepared = await controller.execute(RpcRequest.model_validate({
        "request_id": str(uuid4()), "actor": "swag",
        "action": {"kind": "prepare_force_stop", "profile_id": "minecraft"},
    }))
    controller._bump_generation()
    response = await controller.execute(RpcRequest.model_validate({
        "request_id": str(uuid4()), "actor": "swag",
        "action": {"kind": "confirm_force_stop", "confirmation_id": prepared.result.confirmation_id},
    }))
    assert not response.ok and response.error.code.value == "confirmation_mismatch"


@pytest.mark.asyncio
async def test_lease_failure_cancels_long_phase(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    renewal = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)
    with pytest.raises(_ControllerFailure, match="reservation was lost"):
        await controller._await_lease(asyncio.sleep(10), renewal)


@pytest.mark.asyncio
async def test_adapter_is_called_without_operation_lock(tmp_path) -> None:
    held = False

    class Lock:
        def __enter__(self):
            nonlocal held
            held = True

        def __exit__(self, *_):
            nonlocal held
            held = False

    adapter = _Adapter()
    profile = _profile()
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: adapter},
        operation_lock_factory=Lock,
        await_ready=lambda _profile: None,
    )
    original = adapter.start

    async def checked(profile):
        assert not held
        await original(profile)

    adapter.start = checked
    response = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {"kind": "start", "profile_id": "minecraft"},
            }
        )
    )
    assert response.ok and adapter.started == 1


@pytest.mark.asyncio
async def test_start_cleans_lease_when_job_intent_fails(tmp_path) -> None:
    profile = _profile()
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: _Adapter()},
        operation_lock_factory=lambda: _MemoryLock(),
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
    )
    lease = (ProfileId.MINECRAFT, "lease", 0)
    renewal = asyncio.create_task(asyncio.sleep(3600))
    cleared = []
    async def reserve(*_args, **_kwargs):
        return lease
    controller._reserve = reserve
    controller._lease_renewal = lambda _lease: renewal
    controller._job_intent = lambda *_args: (_ for _ in ()).throw(RuntimeError("db failed"))
    async def clear(current=None):
        cleared.append(current)
    controller._clear_reservation = clear
    with pytest.raises(_ControllerFailure):
        await controller._start(
            Start(kind="start", profile_id=ProfileId.MINECRAFT),
            "swag",
            uuid4(),
        )
    assert renewal.done() and renewal.cancelled()
    assert cleared == [lease]


@pytest.mark.asyncio
async def test_switch_honors_options_and_stops_source_before_target(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(
        update={
            "id": ProfileId.TERRARIA_VANILLA,
            "display_name": "Terraria",
            "operations": frozenset(
                {
                    OperationName.START,
                    OperationName.STOP,
                    OperationName.FORCE_STOP,
                }
            ),
        }
    )
    events = []
    free_budgets = []

    class Adapter(_Adapter):
        async def graceful_stop(self, profile):
            events.append(("stop", profile.id))
            if profile.id is ProfileId.MINECRAFT:
                raise asyncio.TimeoutError

        async def force_stop(self, profile):
            events.append(("force", profile.id))

        async def start(self, profile):
            events.append(("start", profile.id))

    class Backups:
        async def create(self, action, actor=None, request_id=None):
            events.append(("backup", action.profile_id, action.protected))
            return JobAccepted(job_id="backup", state="running")

    class Services:
        backups = Backups()

    async def await_free_slot(timeout):
        free_budgets.append(timeout)
        return True

    controller = Controller(
        profiles={ProfileId.MINECRAFT: source, ProfileId.TERRARIA_VANILLA: target},
        adapters={ProfileId.MINECRAFT: Adapter(), ProfileId.TERRARIA_VANILLA: Adapter()},
        operation_lock_factory=lambda: _MemoryLock(),
        await_free_slot=await_free_slot,
        await_ready=lambda _profile: True,
        services=Services(),
    )
    prepared = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "prepare_switch",
                    "current_profile_id": "minecraft",
                    "target_profile_id": "terraria-vanilla",
                    "options": {
                        "create_backup": True,
                        "force_after_timeout": True,
                        "rollback_on_failure": False,
                    },
                },
            }
        )
    )
    confirmed = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "confirm_switch",
                    "confirmation_id": prepared.result.confirmation_id,
                },
            }
        )
    )
    assert confirmed.ok
    assert events == [
        ("stop", ProfileId.MINECRAFT),
        ("force", ProfileId.MINECRAFT),
        ("backup", ProfileId.MINECRAFT, True),
        ("start", ProfileId.TERRARIA_VANILLA),
    ]
    assert free_budgets == [source.stop_timeout_seconds]


@pytest.mark.asyncio
async def test_switch_rollback_waits_for_free_proof_after_lease_transfer(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(
        update={
            "id": ProfileId.TERRARIA_VANILLA,
            "display_name": "Terraria",
            "operations": frozenset(
                {
                    OperationName.START,
                    OperationName.STOP,
                }
            ),
        }
    )
    events = []
    free = asyncio.Event()
    fail_source_start = True

    class Adapter(_Adapter):
        async def graceful_stop(self, profile):
            events.append(("stop", profile.id))
            free.set()

        async def start(self, profile):
            events.append(("start", profile.id, free.is_set()))
            if profile.id is ProfileId.MINECRAFT and fail_source_start:
                raise RuntimeError("rollback start failed")

    class Store:
        def reserve_if_available(self, *args, **kwargs):
            return None

        def transfer_if_owned(self, profile, operation_id, generation, target, rollback_id):
            events.append(("transfer", profile, target))
            return None

        def release_if_owned(self, *args):
            events.append(("release", args[0]))

    async def await_free_slot():
        await free.wait()
        return True

    controller = Controller(
        profiles={ProfileId.MINECRAFT: source, ProfileId.TERRARIA_VANILLA: target},
        adapters={ProfileId.MINECRAFT: Adapter(), ProfileId.TERRARIA_VANILLA: Adapter()},
        reservation_store=Store(),
        operation_lock_factory=lambda: _MemoryLock(),
        await_free_slot=await_free_slot,
        await_ready=lambda profile: profile.id is ProfileId.MINECRAFT,
    )
    prepared = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "prepare_switch",
                    "current_profile_id": "minecraft",
                    "target_profile_id": "terraria-vanilla",
                    "options": {"rollback_on_failure": True},
                },
            }
        )
    )
    response = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "confirm_switch",
                    "confirmation_id": prepared.result.confirmation_id,
                },
            }
        )
    )
    assert not response.ok

    transfer_index = next(i for i, event in enumerate(events) if event[0] == "transfer")
    source_start_index = next(i for i, event in enumerate(events) if event[0] == "start" and event[1] is ProfileId.MINECRAFT)
    assert transfer_index < source_start_index
    assert events[source_start_index][2] is True
    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='rollback_attempt'"
    ).fetchone()
    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='rollback_failed'"
    ).fetchone()
    assert controller._db().execute(
        "SELECT 1 FROM audit WHERE action='rollback' AND result='failed'"
    ).fetchone()


@pytest.mark.asyncio
async def test_switch_failure_preserves_typed_error_when_renewal_cleanup_raises(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(
        update={
            "id": ProfileId.TERRARIA_VANILLA,
            "display_name": "Terraria",
            "operations": frozenset({OperationName.START, OperationName.STOP}),
        }
    )

    class Adapter(_Adapter):
        pass

    async def faulty_renewal():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise BlockingIOError("reservation ownership changed")

    controller = Controller(
        profiles={ProfileId.MINECRAFT: source, ProfileId.TERRARIA_VANILLA: target},
        adapters={ProfileId.MINECRAFT: Adapter(), ProfileId.TERRARIA_VANILLA: Adapter()},
        operation_lock_factory=lambda: _MemoryLock(),
        await_free_slot=lambda: True,
        await_ready=lambda _profile: False,
    )
    controller._lease_renewal = lambda _lease: asyncio.create_task(faulty_renewal())

    prepared = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "prepare_switch",
                    "current_profile_id": "minecraft",
                    "target_profile_id": "terraria-vanilla",
                    "options": {"rollback_on_failure": False},
                },
            }
        )
    )
    response = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "confirm_switch",
                    "confirmation_id": prepared.result.confirmation_id,
                },
            }
        )
    )

    assert not response.ok
    assert response.error.code.value == "health_failed"


@pytest.mark.asyncio
async def test_reconcile_startup_resolves_stale_pending_claim(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    request_id = uuid4()
    request = RpcRequest.model_validate(
        {
            "request_id": str(request_id),
            "actor": "swag",
            "action": {"kind": "start", "profile_id": "minecraft"},
        }
    )
    canonical = controller._canonical(request)
    controller._db().execute(
        "INSERT INTO rpc_idempotency(request_id,canonical_request,response,status,created_at) VALUES (?,?,?,?,?)",
        (str(request_id), canonical, "", "pending", "2024-01-01T00:00:00Z"),
    )
    controller._db().commit()
    await controller.reconcile_startup()
    response = await controller.execute(request)
    assert not response.ok
    assert response.error.code.value == "internal_error"
    assert response.error.retryable


@pytest.mark.asyncio
async def test_reconcile_startup_autostarts_real_start_action(tmp_path) -> None:
    profile = _profile()
    adapter = _Adapter()
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
        boot_profile="minecraft",
        boot_autostart=True,
    )

    await controller.reconcile_startup()

    assert adapter.started == 1
    job = controller._db().execute(
        "SELECT operation,state FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert job == ("start", "succeeded")


@pytest.mark.asyncio
async def test_reconcile_startup_does_not_start_profile_that_is_already_running(tmp_path) -> None:
    profile = _profile()
    initial = _boot_controller(profile, _Adapter())
    initial._db().execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,completion_seq,detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("already-running", "minecraft", "start", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 1, ""),
    )
    initial._db().commit()
    adapter = _RunningAdapter()
    class Registry(dict):
        @property
        def profiles(self):
            return tuple(self.values())
    restarted = Controller(
        profiles=Registry({profile.id: profile}),
        state_db=initial.state_db,
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
        boot_profile="resume-last-active",
        boot_autostart=True,
    )

    await restarted.reconcile_startup()

    assert adapter.started == 0


@pytest.mark.asyncio
async def test_reconcile_startup_does_not_autostart_after_successful_stop(tmp_path) -> None:
    profile = _profile()
    adapter = _Adapter()
    controller = _boot_controller(profile, adapter)

    await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())
    await controller._stop(Stop(kind="stop", profile_id=profile.id), "test", uuid4())

    restarted_adapter = _Adapter()
    restarted = _boot_controller(profile, restarted_adapter, controller.state_db)
    await restarted.reconcile_startup()

    assert restarted_adapter.started == 0
    assert restarted._resume_profile_id() is None


@pytest.mark.asyncio
async def test_reconcile_startup_does_not_autostart_after_successful_force_stop(tmp_path) -> None:
    profile = _profile().model_copy(
        update={"operations": frozenset({OperationName.START, OperationName.STOP, OperationName.FORCE_STOP})}
    )
    adapter = _Adapter()
    controller = _boot_controller(profile, adapter)

    await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())
    prepared = await controller._prepare_force_stop(
        PrepareForceStop(kind="prepare_force_stop", profile_id=profile.id),
        "test",
        uuid4(),
    )
    await controller._confirm_force_stop(
        ConfirmForceStop(kind="confirm_force_stop", confirmation_id=prepared.confirmation_id),
        "test",
        uuid4(),
    )

    restarted_adapter = _Adapter()
    restarted = _boot_controller(profile, restarted_adapter, controller.state_db)
    await restarted.reconcile_startup()

    assert restarted_adapter.started == 0
    assert restarted._resume_profile_id() is None


@pytest.mark.asyncio
async def test_latest_successful_start_or_switch_remains_resumable(tmp_path) -> None:
    profile = _profile()
    controller = _boot_controller(profile, _Adapter())
    db = controller._db()
    db.executemany(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,completion_seq,detail) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("older-start", "minecraft", "start", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 1, ""),
            ("latest-switch", "minecraft", "switch", "succeeded", "2024-01-02T00:00:00Z", "2024-01-02T00:00:01Z", 2, ""),
        ],
    )
    db.commit()

    assert controller._resume_profile_id() is ProfileId.MINECRAFT


def test_resume_uses_completion_sequence_for_timestamp_ties(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    db = controller._db()
    db.executemany(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,completion_seq,detail) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("stop-row", "minecraft", "stop", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 2, ""),
            ("start-row", "minecraft", "start", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 1, ""),
        ],
    )
    db.commit()

    assert controller._resume_profile_id() is None


def test_resume_uses_latest_completion_across_profiles(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    db = controller._db()
    db.executemany(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,completion_seq,detail) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("minecraft-start", "minecraft", "start", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 3, ""),
            ("pz-switch", "pz-rising", "switch", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", 4, ""),
        ],
    )
    db.commit()

    assert controller._resume_profile_id() is ProfileId.PZ_RISING


def test_resume_fails_closed_for_succeeded_lifecycle_without_completion_sequence(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    db = controller._db()
    db.execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,detail) VALUES (?,?,?,?,?,?,?)",
        ("start-row", "minecraft", "start", "succeeded", "2024-01-01T00:00:00Z", "2024-01-01T00:00:01Z", ""),
    )
    db.commit()

    assert controller._resume_profile_id() is None


@pytest.mark.asyncio
async def test_failed_later_stop_does_not_erase_resumable_start(tmp_path) -> None:
    profile = _profile()
    controller = _boot_controller(profile, _FailingStopAdapter())

    await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())
    with pytest.raises(_ControllerFailure, match="stop failed"):
        await controller._stop(Stop(kind="stop", profile_id=profile.id), "test", uuid4())

    assert controller._db().execute(
        "SELECT operation,state,completion_seq FROM jobs ORDER BY completion_seq"
    ).fetchall() == [
        ("start", "succeeded", 1),
        ("stop", "failed", 2),
    ]

    restarted_adapter = _Adapter()
    restarted = _boot_controller(profile, restarted_adapter, controller.state_db)
    await restarted.reconcile_startup()

    assert restarted_adapter.started == 1
    assert restarted._resume_profile_id() is ProfileId.MINECRAFT


@pytest.mark.asyncio
async def test_slot_conflict_and_confirmation_rejection_are_audited(tmp_path) -> None:
    profile = _profile()
    class Store:
        def reserve_if_available(self, *args, **kwargs):
            raise BlockingIOError
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        reservation_store=Store(),
        operation_lock_factory=lambda: _MemoryLock(),
    )
    response = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {"kind": "start", "profile_id": "minecraft"},
            }
        )
    )
    assert not response.ok and response.error.code.value == "slot_conflict"
    assert controller._db().execute(
        "SELECT 1 FROM audit WHERE actor='swag' AND result='rejected' AND error_code='slot_conflict'"
    ).fetchone()
    mismatch = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "swag",
                "action": {
                    "kind": "confirm_force_stop",
                    "confirmation_id": "x" * 32,
                },
            }
        )
    )
    assert not mismatch.ok
    assert controller._db().execute(
        "SELECT 1 FROM audit WHERE actor='swag' AND result='rejected' AND error_code='confirmation_mismatch'"
    ).fetchone()


def test_event_deduplication_is_limited_to_incident_window(tmp_path) -> None:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    controller = Controller.for_testing(tmp_path)
    controller._clock = lambda: now
    asyncio.run(controller._record_event(ProfileId.MINECRAFT, "slot_conflict", "busy"))
    asyncio.run(controller._record_event(ProfileId.MINECRAFT, "slot_conflict", "busy"))
    now = now + timedelta(minutes=6)
    asyncio.run(controller._record_event(ProfileId.MINECRAFT, "slot_conflict", "busy"))
    assert controller._db().execute(
        "SELECT COUNT(*) FROM events WHERE code='slot_conflict'"
    ).fetchone()[0] == 2


@pytest.mark.asyncio
async def test_prepare_operations_require_profile_gates(tmp_path) -> None:
    profile = _profile().model_copy(update={"operations": frozenset()})
    controller = Controller(
        profiles={
            ProfileId.MINECRAFT: profile,
            ProfileId.TERRARIA_VANILLA: profile.model_copy(
                update={"id": ProfileId.TERRARIA_VANILLA, "operations": frozenset()}
            ),
            ProfileId.TERRARIA_TMOD: profile.model_copy(
                update={"id": ProfileId.TERRARIA_TMOD, "operations": frozenset()}
            ),
        },
        operation_lock_factory=lambda: _MemoryLock(),
    )
    actions = (
        {"kind": "create_backup", "profile_id": "minecraft"},
        {"kind": "prepare_restore", "profile_id": "minecraft", "backup_id": "backup"},
        {"kind": "check_update", "profile_id": "minecraft"},
        {"kind": "prepare_update", "profile_id": "minecraft"},
        {
            "kind": "prepare_world_clone",
            "source_world_id": "world",
            "destination_name": "copy",
        },
    )
    for action in actions:
        response = await controller.execute(
            RpcRequest.model_validate(
                {"request_id": str(uuid4()), "actor": "swag", "action": action}
            )
        )
        assert not response.ok and response.error.code.value == "invalid_request"


@pytest.mark.asyncio
async def test_start_slot_conflict_finishes_accepted_job_as_failed(tmp_path) -> None:
    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: _Adapter()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: False,
    )

    with pytest.raises(_ControllerFailure, match="slot is occupied"):
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert controller._db().execute(
        "SELECT operation,state,detail FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone() == ("start", "failed", "slot is occupied")


@pytest.mark.asyncio
async def test_start_async_readiness_failure_finishes_job(tmp_path) -> None:
    profile = _profile()

    async def not_ready(_profile):
        return False

    adapter = _Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=not_ready,
    )

    with pytest.raises(_ControllerFailure, match="start failed"):
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert controller._db().execute(
        "SELECT operation,state,detail FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone() == ("start", "failed", "start failed")
    assert controller._readiness.health()["failure"] == 1
    assert controller._readiness.health()["active"] == 0
    assert adapter.forced == 1
    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='start_cleanup_succeeded'"
    ).fetchone()


@pytest.mark.asyncio
async def test_start_timeout_finishes_job_with_retryable_timeout(tmp_path) -> None:
    profile = _profile()

    async def times_out(_profile):
        raise asyncio.TimeoutError

    adapter = _Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=times_out,
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert exc_info.value.code.value == "start_timeout"
    assert controller._db().execute(
        "SELECT operation,state,detail FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone() == ("start", "failed", "start timed out")
    assert controller._readiness.health()["timeout"] == 1
    assert controller._readiness.health()["active"] == 0
    assert adapter.forced == 1


@pytest.mark.asyncio
async def test_start_cleanup_failure_does_not_replace_primary_failure(tmp_path) -> None:
    class CleanupFails(_Adapter):
        async def force_stop(self, profile):
            raise RuntimeError("cleanup exploded")

    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: CleanupFails()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: False,
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert exc_info.value.code is ErrorCode.HEALTH_FAILED
    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='start_cleanup_failed'"
    ).fetchone()


@pytest.mark.asyncio
async def test_start_command_failure_also_runs_bounded_cleanup(tmp_path) -> None:
    class StartFails(_Adapter):
        async def start(self, profile):
            raise RuntimeError("partial start")

    profile = _profile()
    adapter = StartFails()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert exc_info.value.code is ErrorCode.HEALTH_FAILED
    assert adapter.forced == 1


@pytest.mark.asyncio
async def test_start_cleanup_event_failure_does_not_replace_primary_failure(tmp_path) -> None:
    profile = _profile()
    adapter = _Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: False,
    )

    async def event_store_failed(*_args, **_kwargs):
        raise RuntimeError("event store unavailable")

    controller._record_event = event_store_failed
    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._start(Start(kind="start", profile_id=profile.id), "test", uuid4())

    assert exc_info.value.code is ErrorCode.HEALTH_FAILED
    assert adapter.forced == 1


@pytest.mark.asyncio
async def test_stop_timeout_finishes_job_with_grace_timeout(tmp_path) -> None:
    class TimeoutAdapter(_Adapter):
        async def graceful_stop(self, profile):
            raise asyncio.TimeoutError

    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: TimeoutAdapter()},
        operation_lock_factory=_MemoryLock,
    )

    with pytest.raises(_ControllerFailure, match="graceful stop timed out"):
        await controller._stop(Stop(kind="stop", profile_id=profile.id), "test", uuid4())

    assert controller._db().execute(
        "SELECT operation,state,detail FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone() == ("stop", "failed", "graceful stop timed out")


@pytest.mark.asyncio
async def test_reserve_fallback_seam_keeps_check_and_reserve_under_lock(tmp_path) -> None:
    class Store:
        def __init__(self):
            self.calls = []

        def read(self):
            self.calls.append("read")
            return None

        def reserve(self, profile_id, lease_id, ttl):
            self.calls.append((profile_id, lease_id, ttl))

    profile = _profile()
    store = Store()
    controller = Controller(
        profiles={profile.id: profile},
        reservation_store=store,
        operation_lock_factory=_MemoryLock,
    )

    lease = await controller._reserve(profile, "start", "operation")

    assert lease[0:2] == (profile.id, "operation")
    assert store.calls == ["read", (profile.id, "operation", 30.0)]


@pytest.mark.asyncio
async def test_reserve_atomic_seam_falls_back_without_generation_keyword(tmp_path) -> None:
    class Store:
        def __init__(self):
            self.calls = []

        def reserve_if_available(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if kwargs:
                raise TypeError("legacy reservation seam")

    profile = _profile()
    store = Store()
    controller = Controller(
        profiles={profile.id: profile},
        reservation_store=store,
        operation_lock_factory=_MemoryLock,
    )

    lease = await controller._reserve(profile, "start", "operation")

    assert lease[0:2] == (profile.id, "operation")
    assert len(store.calls) == 2


@pytest.mark.asyncio
async def test_reserve_fallback_rejects_another_profile_owner(tmp_path) -> None:
    class Store:
        def read(self):
            return SimpleNamespace(profile_id=ProfileId.TERRARIA_VANILLA)

        def reserve(self, *_args):
            raise AssertionError("must not reserve over another owner")

    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        reservation_store=Store(),
        operation_lock_factory=_MemoryLock,
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._reserve(profile, "start", "operation")

    assert exc_info.value.code.value == "slot_conflict"


@pytest.mark.asyncio
async def test_clear_reservation_exercises_owned_clear_and_path_cleanup(tmp_path) -> None:
    profile = ProfileId.MINECRAFT
    lease = (profile, "operation", 0)

    class Releaser:
        def __init__(self):
            self.args = None

        async def release_if_owned(self, *args):
            self.args = args

    releaser = Releaser()
    controller = Controller.for_testing(tmp_path)
    controller.reservation_store = releaser
    await controller._clear_reservation(lease)
    assert releaser.args == lease

    class Clearer:
        def __init__(self):
            self.cleared = False

        async def clear(self):
            self.cleared = True

    clearer = Clearer()
    controller.reservation_store = clearer
    await controller._clear_reservation()
    assert clearer.cleared

    reservation_path = tmp_path / "reservation"
    reservation_path.write_text("lease")
    controller.reservation_store = SimpleNamespace(reservation_path=reservation_path)
    await controller._clear_reservation()
    assert not reservation_path.exists()


@pytest.mark.asyncio
async def test_clear_reservation_ignores_path_unlink_failure(tmp_path, monkeypatch) -> None:
    reservation_path = tmp_path / "reservation"
    reservation_path.write_text("lease")
    controller = Controller.for_testing(tmp_path)
    controller.reservation_store = SimpleNamespace(reservation_path=reservation_path)

    def fail_unlink(self, *, missing_ok=False):
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    await controller._clear_reservation()


@pytest.mark.asyncio
async def test_switch_timeout_without_force_preserves_typed_error(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(update={"id": ProfileId.TERRARIA_VANILLA, "display_name": "Terraria"})

    class Adapter(_Adapter):
        async def graceful_stop(self, profile):
            if profile.id is ProfileId.MINECRAFT:
                raise asyncio.TimeoutError

    controller = Controller(
        profiles={source.id: source, target.id: target},
        adapters={source.id: Adapter(), target.id: Adapter()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
    )
    prepared = await controller._prepare_switch(
        PrepareSwitch(
            kind="prepare_switch",
            current_profile_id=source.id,
            target_profile_id=target.id,
            options=SwitchOptions(force_after_timeout=False, rollback_on_failure=False),
        ),
        "test",
        uuid4(),
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._confirm_switch(
            ConfirmSwitch(kind="confirm_switch", confirmation_id=prepared.confirmation_id),
            "test",
            uuid4(),
        )

    assert exc_info.value.code.value == "grace_timeout"


@pytest.mark.asyncio
async def test_switch_async_target_readiness_rolls_back_successfully(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(update={"id": ProfileId.TERRARIA_VANILLA, "display_name": "Terraria"})
    events = []

    class Adapter(_Adapter):
        async def graceful_stop(self, profile):
            events.append(("stop", profile.id))

        async def start(self, profile):
            events.append(("start", profile.id))

    class Store:
        def reserve_if_available(self, *_args, **_kwargs):
            return None

        async def transfer_if_owned(self, profile, operation_id, generation, target, rollback_id):
            del operation_id, generation, rollback_id
            events.append(("transfer", profile, target))
            return None

        def release_if_owned(self, *_args):
            return None

    async def ready(profile):
        return profile.id is ProfileId.MINECRAFT

    controller = Controller(
        profiles={source.id: source, target.id: target},
        adapters={source.id: Adapter(), target.id: Adapter()},
        reservation_store=Store(),
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=ready,
    )
    prepared = await controller._prepare_switch(
        PrepareSwitch(
            kind="prepare_switch",
            current_profile_id=source.id,
            target_profile_id=target.id,
            options=SwitchOptions(rollback_on_failure=True),
        ),
        "test",
        uuid4(),
    )

    with pytest.raises(_ControllerFailure, match="switch failed"):
        await controller._confirm_switch(
            ConfirmSwitch(kind="confirm_switch", confirmation_id=prepared.confirmation_id),
            "test",
            uuid4(),
        )

    assert events == [
        ("stop", source.id),
        ("start", target.id),
        ("stop", target.id),
        ("transfer", target.id, source.id),
        ("start", source.id),
    ]
    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='rollback_succeeded'"
    ).fetchone()
    assert ("transfer", target.id, source.id) in events


@pytest.mark.asyncio
async def test_start_with_free_retry_retries_controller_busy_exit(tmp_path) -> None:
    class BusyStart(Exception):
        returncode = 75

    class Adapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def start(self, profile):
            self.attempts += 1
            if self.attempts == 1:
                raise BusyStart

    profile = _profile()
    adapter = Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
    )

    await controller._start_with_free_retry(profile)

    assert adapter.attempts == 2


@pytest.mark.asyncio
async def test_switch_fails_when_slot_never_becomes_free(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(update={"id": ProfileId.TERRARIA_VANILLA, "display_name": "Terraria"})
    controller = Controller(
        profiles={source.id: source, target.id: target},
        adapters={source.id: _Adapter(), target.id: _Adapter()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: False,
        await_ready=lambda _profile: True,
    )
    prepared = await controller._prepare_switch(
        PrepareSwitch(
            kind="prepare_switch",
            current_profile_id=source.id,
            target_profile_id=target.id,
            options=SwitchOptions(rollback_on_failure=False),
        ),
        "test",
        uuid4(),
    )

    with pytest.raises(_ControllerFailure) as exc_info:
        await controller._confirm_switch(
            ConfirmSwitch(kind="confirm_switch", confirmation_id=prepared.confirmation_id),
            "test",
            uuid4(),
        )

    assert exc_info.value.code.value == "slot_conflict"


@pytest.mark.asyncio
async def test_switch_records_failed_rollback_when_source_is_not_ready(tmp_path) -> None:
    source = _profile()
    target = source.model_copy(update={"id": ProfileId.TERRARIA_VANILLA, "display_name": "Terraria"})
    controller = Controller(
        profiles={source.id: source, target.id: target},
        adapters={source.id: _Adapter(), target.id: _Adapter()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: False,
    )
    prepared = await controller._prepare_switch(
        PrepareSwitch(
            kind="prepare_switch",
            current_profile_id=source.id,
            target_profile_id=target.id,
            options=SwitchOptions(rollback_on_failure=True),
        ),
        "test",
        uuid4(),
    )

    with pytest.raises(_ControllerFailure, match="switch failed"):
        await controller._confirm_switch(
            ConfirmSwitch(kind="confirm_switch", confirmation_id=prepared.confirmation_id),
            "test",
            uuid4(),
        )

    assert controller._db().execute(
        "SELECT 1 FROM events WHERE code='rollback_failed'"
    ).fetchone()


@pytest.mark.asyncio
async def test_start_with_free_retry_reports_slot_conflict_if_retry_stays_busy(tmp_path) -> None:
    class BusyStart(Exception):
        returncode = 75

    class Adapter(_Adapter):
        async def start(self, profile):
            raise BusyStart

    profile = _profile()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: Adapter()},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: False,
    )

    with pytest.raises(_ControllerFailure, match="slot did not become free"):
        await controller._start_with_free_retry(profile)


@pytest.mark.asyncio
async def test_start_with_free_retry_renews_lease_for_second_attempt(tmp_path) -> None:
    class BusyStart(Exception):
        returncode = 75

    class Adapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def start(self, profile):
            self.attempts += 1
            if self.attempts == 1:
                raise BusyStart

    profile = _profile()
    adapter = Adapter()
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=_MemoryLock,
        await_free_slot=lambda: True,
    )
    renewal = asyncio.create_task(asyncio.Event().wait())

    try:
        await controller._start_with_free_retry(profile, renewal)
    finally:
        renewal.cancel()
        with pytest.raises(asyncio.CancelledError):
            await renewal

    assert adapter.attempts == 2


@pytest.mark.asyncio
async def test_force_stop_failure_finishes_job_as_failed(tmp_path) -> None:
    class FailingForceStop(_Adapter):
        async def force_stop(self, profile):
            raise RuntimeError("force stop failed")

    profile = _profile().model_copy(
        update={"operations": frozenset({OperationName.START, OperationName.STOP, OperationName.FORCE_STOP})}
    )
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: FailingForceStop()},
        operation_lock_factory=_MemoryLock,
    )
    prepared = await controller._prepare_force_stop(
        PrepareForceStop(kind="prepare_force_stop", profile_id=profile.id), "test", uuid4()
    )

    with pytest.raises(_ControllerFailure, match="force stop failed"):
        await controller._confirm_force_stop(
            ConfirmForceStop(kind="confirm_force_stop", confirmation_id=prepared.confirmation_id),
            "test",
            uuid4(),
        )

    assert controller._db().execute(
        "SELECT operation,state,detail FROM jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone() == ("force_stop", "failed", "force stop failed")


@pytest.mark.asyncio
async def test_await_lease_without_renewal_runs_work(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)

    async def immediate():
        return "done"

    assert await controller._await_lease(immediate(), None) == "done"


@pytest.mark.asyncio
async def test_cancelled_threaded_lease_drains_worker_before_cleanup(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def worker():
        started.set()
        release.wait(timeout=2)
        return "mutated"

    renewal = asyncio.create_task(asyncio.sleep(3600))
    task = asyncio.create_task(controller._await_lease(asyncio.to_thread(worker), renewal))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for the threaded mutation to finish"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal


@pytest.mark.asyncio
async def test_confirmation_consumption_and_expiry_are_rejected(tmp_path) -> None:
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    profile = _profile().model_copy(
        update={"operations": frozenset({OperationName.START, OperationName.STOP, OperationName.FORCE_STOP})}
    )
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: _Adapter()},
        operation_lock_factory=_MemoryLock,
        clock=lambda: now,
    )

    consumed = await controller._prepare_force_stop(
        PrepareForceStop(kind="prepare_force_stop", profile_id=profile.id), "test", uuid4()
    )
    await controller._consume_confirmation("test", "force_stop", consumed.confirmation_id)
    with pytest.raises(_ControllerFailure, match="already consumed"):
        await controller._consume_confirmation("test", "force_stop", consumed.confirmation_id)

    expired = await controller._prepare_force_stop(
        PrepareForceStop(kind="prepare_force_stop", profile_id=profile.id), "test", uuid4()
    )
    controller._clock = lambda: now + timedelta(minutes=6)
    with pytest.raises(_ControllerFailure, match="confirmation expired"):
        await controller._consume_confirmation("test", "force_stop", expired.confirmation_id)


@pytest.mark.asyncio
async def test_reconcile_startup_marks_jobs_and_reconciles_slot_observation(tmp_path) -> None:
    profile = _profile()

    class Registry(dict):
        @property
        def profiles(self):
            return tuple(self.values())

    class Reservation:
        def __init__(self):
            self.reconciled = False

        async def reconcile(self):
            self.reconciled = True
            return True

    class Adapter(_Adapter):
        async def observe(self, profile):
            raise RuntimeError("observation failed")

        async def start(self, profile):
            raise RuntimeError("boot start failed")

    class Inspector:
        def __init__(self):
            self.observed = 0

        def observe(self):
            self.observed += 1

    reservation = Reservation()
    inspector = Inspector()
    controller = Controller(
        profiles=Registry({profile.id: profile}),
        reservation_store=reservation,
        adapters={profile.id: Adapter()},
        operation_lock_factory=_MemoryLock,
        slot_inspector=inspector,
        await_free_slot=lambda: True,
        await_ready=lambda _profile: True,
        boot_profile=profile.id.value,
        boot_autostart=True,
    )
    controller._db().execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,detail) VALUES (?,?,?,?,?,?)",
        ("pending", profile.id.value, "start", "accepted", "2024-01-01T00:00:00Z", ""),
    )
    controller._db().execute(
        "CREATE TABLE benchmark_runs("
        "id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,baseline_preset TEXT NOT NULL,"
        "candidate_preset TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,"
        "finished_at TEXT,error_code TEXT)"
    )
    controller._db().execute(
        "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("bench-pending", profile.id.value, "current", "candidate", "running", "2024-01-01T00:00:00Z"),
    )
    controller._db().commit()

    await controller.reconcile_startup()

    assert reservation.reconciled
    assert inspector.observed == 1
    assert controller._boot_autostart_attempted
    assert controller._db().execute(
        "SELECT state,detail FROM jobs WHERE id='pending'"
    ).fetchone() == ("failed", "controller restarted")
    assert controller._db().execute(
        "SELECT state,error_code FROM benchmark_runs WHERE id='bench-pending'"
    ).fetchone() == ("failed", "controller_restarted")


@pytest.mark.asyncio
async def test_aclose_cancels_retained_workers_and_waits_for_worker_cleanup(tmp_path):
    controller = Controller.for_testing(tmp_path)
    cleanup = asyncio.Event()

    async def worker():
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()

    task = asyncio.create_task(worker())
    controller._background_tasks.add(task)
    task.add_done_callback(controller._consume_background_task)
    closing = asyncio.create_task(controller.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert cleanup.is_set()
    assert task.done()
    await controller.aclose()
