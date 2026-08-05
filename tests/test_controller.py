from uuid import uuid4
from pathlib import Path
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from game_control.controller import Controller, dispatch_is_exhaustive, _ControllerFailure
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
    GetStatus,
    JobAccepted,
    PrepareForceStop,
    RpcRequest,
    Start,
    Stop,
)


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


class _MemoryLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_dispatch_is_exhaustive() -> None:
    assert dispatch_is_exhaustive()


def test_controller_deduplicates_exact_replay(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    request = RpcRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "actor": "operator",
            "action": {"kind": "get_status"},
        }
    )
    first = controller.execute_sync(request)
    second = controller.execute_sync(request)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


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
        {"request_id": str(request_id), "actor": "operator", "action": {"kind": "get_status"}}
    )
    second = RpcRequest.model_validate(
        {"request_id": str(request_id), "actor": "operator", "action": {"kind": "get_profiles"}}
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
        "request_id": str(uuid4()), "actor": "operator",
        "action": {"kind": "start", "profile_id": "minecraft"},
    })
    responses = await asyncio.gather(controller.execute(request), controller.execute(request))
    assert responses[0].model_dump(mode="json") == responses[1].model_dump(mode="json")
    assert adapter.started == 1


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
        "request_id": str(uuid4()), "actor": "operator",
        "action": {"kind": "prepare_force_stop", "profile_id": "minecraft"},
    }))
    controller._bump_generation()
    response = await controller.execute(RpcRequest.model_validate({
        "request_id": str(uuid4()), "actor": "operator",
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
                "actor": "operator",
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
            "operator",
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
                "actor": "operator",
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
                "actor": "operator",
                "action": {
                    "kind": "confirm_switch",
                    "confirmation_id": prepared.result.confirmation_id,
                },
            }
        )
    )
    assert confirmed.ok
    assert events == [
        ("backup", ProfileId.MINECRAFT, True),
        ("stop", ProfileId.MINECRAFT),
        ("force", ProfileId.MINECRAFT),
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
                "actor": "operator",
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
                "actor": "operator",
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
async def test_reconcile_startup_resolves_stale_pending_claim(tmp_path) -> None:
    controller = Controller.for_testing(tmp_path)
    request_id = uuid4()
    canonical = '{"action":{"kind":"get_status"},"actor":"operator","request_id":"' + str(request_id) + '"}'
    controller._db().execute(
        "INSERT INTO rpc_idempotency(request_id,canonical_request,response,status,created_at) VALUES (?,?,?,?,?)",
        (str(request_id), canonical, "", "pending", "2024-01-01T00:00:00Z"),
    )
    controller._db().commit()
    await controller.reconcile_startup()
    response = await controller.execute(
        RpcRequest.model_validate(
            {"request_id": str(request_id), "actor": "operator", "action": {"kind": "get_status"}}
        )
    )
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
                "actor": "operator",
                "action": {"kind": "start", "profile_id": "minecraft"},
            }
        )
    )
    assert not response.ok and response.error.code.value == "slot_conflict"
    assert controller._db().execute(
        "SELECT 1 FROM audit WHERE actor='operator' AND result='rejected' AND error_code='slot_conflict'"
    ).fetchone()
    mismatch = await controller.execute(
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "operator",
                "action": {
                    "kind": "confirm_force_stop",
                    "confirmation_id": "x" * 32,
                },
            }
        )
    )
    assert not mismatch.ok
    assert controller._db().execute(
        "SELECT 1 FROM audit WHERE actor='operator' AND result='rejected' AND error_code='confirmation_mismatch'"
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
                {"request_id": str(uuid4()), "actor": "operator", "action": action}
            )
        )
        assert not response.ok and response.error.code.value == "invalid_request"
