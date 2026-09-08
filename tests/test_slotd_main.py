from __future__ import annotations

import asyncio
import stat
import logging
import socket
import warnings
from types import SimpleNamespace
from uuid import uuid4

import pytest

from game_control.protocol import (
    MAX_REQUEST_BYTES,
    ConfirmSwitch,
    ErrorCode,
    GetPerf,
    RpcRequest,
    failure,
    response_from_json,
    PerfAggregate,
    PerfSnapshot,
    RpcSuccess,
    StatusSnapshot,
)
from game_control.push import WatchProtocolError
from game_control.slot import SlotObservation
from game_control.models import AdapterKind, OperationName, ProfileId
from game_control.adapters.crafty import CraftyAdapter
from game_control.controller import Controller
from game_control.history_queries import HistoryQueryService
from game_control.notifications import NotificationService
from game_control.rcon_telemetry import PersistentRconTelemetry
from game_control.runtime.alerts import AlertRuntime
from game_control.runtime.telemetry import TelemetryRuntime
from game_control.telemetry_db import TelemetryDatabase
from game_control.updates import UpdateService
import game_control.slotd_main as slotd_main
import game_control.state_db as state_db_module
import game_control.service_wiring as service_wiring
from game_control.slotd_main import UnixRpcServer, _await_free_slot, _await_systemd_profile_ready, _maintenance_loop


class _TestAssembly:
    def __init__(self, controller):
        self.controller = controller

    async def aclose(self):
        first_error = None
        sampler = getattr(self.controller.services, "tps_sampler", None)
        if sampler is not None:
            try:
                await sampler.aclose()
            except BaseException as exc:
                first_error = exc
        close = getattr(self.controller.services, "close", None)
        if close is not None:
            try:
                await close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


async def _test_assembly(controller):
    return _TestAssembly(controller)


@pytest.mark.asyncio
async def test_provisional_owner_closes_registered_resources_once_in_reverse_order():
    events = []
    owner = slotd_main._ProvisionalOwner()
    owner.register("state", lambda: events.append("state"))
    owner.register("service", lambda: events.append("service"))
    owner.register("service", lambda: events.append("duplicate"))

    await owner.aclose()
    await owner.aclose()

    assert events == ["service", "state"]


@pytest.mark.asyncio
async def test_assembly_cancellation_keeps_primary_error_when_cleanup_fails(monkeypatch):
    events = []

    class Owned:
        def close(self):
            events.append("close")
            raise RuntimeError("cleanup failed")

    def fail_build(_config, *, provisional):
        resource = Owned()
        provisional.register(resource, resource.close)
        raise asyncio.CancelledError

    monkeypatch.setattr(slotd_main, "_build_controller_unmanaged", fail_build)
    with pytest.raises(asyncio.CancelledError):
        await slotd_main.build_controller_assembly()
    assert events == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_boundary, cancellation",
    [
        (boundary, cancellation)
        for boundary in (
            "state_db", "crafty", "one_shot_rcon", "persistent_rcon",
            "telemetry_db", "notification", "alert", "telemetry_runtime",
            "update:terraria-vanilla", "update:minecraft-sunlit-cobblemon",
            "history", "service_seams", "service_seams_return", "session_recovery",
            "controller", "container", "finalizer",
        )
        for cancellation in (False, True)
    ],
)
async def test_production_assembly_failure_matrix_closes_each_acquisition_once(
    monkeypatch, tmp_path, failure_boundary, cancellation
):
    """Fault every real root seam and verify provisional ownership is exact."""
    class Registry:
        def __iter__(self):
            return iter((terraria, sunlit))

    terraria = SimpleNamespace(
        id=ProfileId.TERRARIA_VANILLA, display_name="Terraria", adapter=AdapterKind.CRAFTY,
        crafty_server_id=1, process=SimpleNamespace(executable="/usr/bin/game", argv_contains=()),
        ports=(SimpleNamespace(protocol="tcp", port=7777, required=True),),
        paths=SimpleNamespace(data_roots=("/var/lib/game",), mutable_root="/var/lib/game", log_files=(),
                              backup_root="/var/backups/game", install_root="/opt/game", version_file="/var/lib/game/version"),
        operations=frozenset(OperationName), update=SimpleNamespace(kind="manual"),
        notification_events=frozenset(), health_timeout_seconds=5,
    )
    sunlit = SimpleNamespace(
        id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON, display_name="Sunlit", adapter=AdapterKind.SYSTEMD,
        process=SimpleNamespace(executable="/usr/bin/game", argv_contains=()),
        ports=(SimpleNamespace(protocol="tcp", port=25565, required=True),),
        paths=SimpleNamespace(data_roots=("/var/lib/game",), mutable_root="/var/lib/game", log_files=(),
                              backup_root="/var/backups/game", install_root="/opt/game", version_file="/var/lib/game/version"),
        operations=frozenset(OperationName), update=SimpleNamespace(kind="manual"),
        notification_events=frozenset(), health_timeout_seconds=5,
    )
    config = tmp_path / "root.toml"
    token = tmp_path / "crafty-token"
    token.write_text("test-token", encoding="utf-8")
    config.write_text(
        f'profiles_dir = "{tmp_path}"\nstate_db = "{tmp_path / "state.db"}"\n'
        f'reservation_path = "{tmp_path / "reservation.json"}"\n'
        f'[crafty]\nbase_url = "http://127.0.0.1:8000"\ntoken_path = "{token}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(slotd_main.ProfileRegistry, "load", staticmethod(lambda _path: Registry()))
    monkeypatch.setattr(state_db_module, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(slotd_main, "STATE_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(service_wiring, "_AUDIT_STATE_DB_PATH", tmp_path / "state.db")

    events = []
    def wrap_sync(cls, method, label):
        original = getattr(cls, method)
        def wrapped(self, *args, **kwargs):
            events.append(label)
            if cancellation and label == "state_db":
                raise RuntimeError("injected cleanup failure")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(cls, method, wrapped)

    def wrap_async(cls, method, label):
        original = getattr(cls, method)
        async def wrapped(self, *args, **kwargs):
            events.append(label)
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(cls, method, wrapped)

    wrap_sync(state_db_module.StateDatabase, "close", "state_db")
    wrap_async(CraftyAdapter, "aclose", "crafty")
    wrap_async(PersistentRconTelemetry, "close", "persistent_rcon")
    wrap_sync(TelemetryDatabase, "close", "telemetry_db")
    wrap_sync(NotificationService, "close", "notification")
    wrap_async(AlertRuntime, "close", "alert")
    wrap_async(TelemetryRuntime, "close", "telemetry_runtime")
    wrap_async(UpdateService, "aclose", "update")
    wrap_async(HistoryQueryService, "aclose", "history")
    wrap_async(Controller, "aclose", "controller")

    acquired = []
    def fail_at(label):
        acquired.append(label)
        if label == failure_boundary:
            if cancellation:
                raise asyncio.CancelledError
            raise RuntimeError(f"injected boundary failure: {label}")

    expected_exception = asyncio.CancelledError if cancellation else RuntimeError
    with pytest.raises(expected_exception, match=None if cancellation else "injected boundary failure"):
        await slotd_main.build_controller_assembly(config, _boundary_hook=fail_at)

    # A one-shot RCON has no persistent close operation; its explicit owner
    # hook is still exercised, while persistent RCON is closed exactly once.
    expected = []
    for label in reversed(acquired):
        if label in {"service_seams", "service_seams_return", "session_recovery", "container", "finalizer"}:
            continue
        if label == "one_shot_rcon":
            continue
        if label == "telemetry_db" and "telemetry_runtime" in acquired:
            continue  # TelemetryRuntime is the sole delegated database owner.
        if label == "persistent_rcon" and "telemetry_runtime" in acquired:
            continue  # TelemetryRuntime is also the sole delegated RCON owner.
        if label.startswith("update:"):
            label = "update"
        expected.append(label)
        if label == "telemetry_runtime" and "telemetry_db" in acquired:
            if "persistent_rcon" in acquired:
                expected.append("persistent_rcon")
            expected.append("telemetry_db")
    assert events == expected


@pytest.mark.asyncio
async def test_serve_closes_partial_task_creation_without_pending_tasks(monkeypatch):
    class Services:
        tps_sampler = None

        async def close(self):
            return None

    class Controller:
        services = Services()
        profiles = ()
        adapters = {}
        slot_inspector = None

        async def reconcile_startup(self):
            return None

        async def maintenance_tick(self):
            await asyncio.sleep(3600)

    class Server:
        def __init__(self, _controller):
            self._server = self
            self.closed = False

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(3600)

        async def close(self):
            self.closed = True

    server = None
    calls = 0
    real_create_task = asyncio.create_task

    def create_task(coro, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            coro.close()
            raise RuntimeError("server task creation failed")
        return real_create_task(coro, *args, **kwargs)

    def make_server(controller):
        nonlocal server
        server = Server(controller)
        return server

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", make_server)
    monkeypatch.setattr(slotd_main.asyncio, "create_task", create_task)

    with pytest.raises(RuntimeError, match="server task creation failed"):
        await slotd_main.serve()

    assert server is not None and server.closed is True


@pytest.mark.asyncio
async def test_serve_closes_supervisor_coroutines_at_every_creation_point(monkeypatch):
    class Sampler:
        def start(self):
            return asyncio.create_task(asyncio.sleep(3600))

    class LegacySampler:
        async def run(self, _running):
            await asyncio.sleep(3600)

        async def aclose(self):
            return None

    class Services:
        telemetry_sampler = Sampler()
        tps_sampler = LegacySampler()

        async def close(self):
            return None

    class Controller:
        services = Services()
        profiles = ()
        adapters = {}
        slot_inspector = None

        async def reconcile_startup(self):
            return None

        async def maintenance_tick(self):
            await asyncio.sleep(3600)

    class Server:
        def __init__(self, _controller):
            self._server = self

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(3600)

        async def close(self):
            return None

    original_create_task = asyncio.create_task
    for failure_point in range(1, 7):
        calls = 0
        created_names = []

        def create_task(coro, *args, **kwargs):
            nonlocal calls
            calls += 1
            created_names.append(kwargs.get("name"))
            if calls == failure_point:
                coro.close()
                raise RuntimeError("injected task creation failure")
            return original_create_task(coro, *args, **kwargs)

        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
        monkeypatch.setattr(slotd_main, "UnixRpcServer", Server)
        monkeypatch.setattr(slotd_main.asyncio, "create_task", create_task)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(RuntimeError, match="injected task creation failure"):
                await slotd_main.serve()
        loop.set_exception_handler(previous_handler)
        assert not [warning for warning in caught if "never awaited" in str(warning.message)]
        assert not loop_errors
        assert created_names[failure_point - 1] == (
            "horizon-initialization",
            "horizon-maintenance-supervisor",
            "horizon-event-loop-lag-supervisor",
            "horizon-telemetry-supervisor",
            "horizon-legacy-tps-supervisor",
            "horizon-rpc-supervisor",
        )[failure_point - 1]


@pytest.mark.asyncio
async def test_serve_observes_completed_supervisor_exception_during_cancel_race(monkeypatch):
    class Services:
        tps_sampler = None
        telemetry_sampler = None

        async def close(self):
            return None

    class Controller:
        services = Services()
        profiles = ()
        adapters = {}
        slot_inspector = None

        async def reconcile_startup(self):
            return None

        async def maintenance_tick(self):
            await asyncio.sleep(3600)

    class Server:
        def __init__(self, _controller):
            self._server = self

        async def start(self):
            return None

        async def serve_forever(self):
            raise RuntimeError("unobserved supervisor")

        async def close(self):
            return None

    real_wait = asyncio.wait
    raced = False

    async def race_wait(aws, *args, **kwargs):
        nonlocal raced
        done, pending = await real_wait(aws, *args, **kwargs)
        if not raced and done:
            raced = True
            asyncio.current_task().cancel()
            return set(), set(aws)
        return done, pending

    loop = asyncio.get_running_loop()
    loop_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", Server)
    monkeypatch.setattr(slotd_main.asyncio, "wait", race_wait)
    try:
        with pytest.raises(asyncio.CancelledError):
            await slotd_main.serve()
    finally:
        loop.set_exception_handler(previous_handler)
    await asyncio.sleep(0)
    assert not loop_errors


class _Reader:
    def __init__(self, request: RpcRequest):
        self.request = (request.model_dump_json() + "\n").encode()

    async def readuntil(self, _separator: bytes) -> bytes:
        return self.request


@pytest.mark.asyncio
async def test_systemd_readiness_waits_for_slot_validated_process_and_listener():
    profile = SimpleNamespace(id=SimpleNamespace(value="minecraft-sunlit-cobblemon"))
    statuses = iter(
        (
            SimpleNamespace(
                profile_id=profile.id,
                health="healthy",
                pid=101,
                required_ports_ready=True,
            ),
            SimpleNamespace(
                profile_id=profile.id,
                health="unknown",
                pid=None,
                required_ports_ready=False,
            ),
            SimpleNamespace(
                profile_id=profile.id,
                health="healthy",
                pid=101,
                required_ports_ready=True,
            ),
        )
    )
    slots = iter(
        (
            SimpleNamespace(owner=None, pid=None, inconsistent=False),
            SimpleNamespace(owner=profile.id, pid=101, inconsistent=False),
            SimpleNamespace(owner=profile.id, pid=101, inconsistent=False),
        )
    )

    class Status:
        async def snapshot(self):
            return SimpleNamespace(profiles=(next(statuses),))

    inspector = SimpleNamespace(observe=lambda: next(slots))
    assert await _await_systemd_profile_ready(
        Status(), inspector, profile, 1, poll_interval=0
    ) is True


@pytest.mark.asyncio
async def test_systemd_readiness_rejects_transient_systemd_active_state():
    profile = SimpleNamespace(id=SimpleNamespace(value="minecraft-sunlit-cobblemon"))
    transient = SimpleNamespace(
        profile_id=profile.id,
        health="healthy",
        pid=101,
        required_ports_ready=True,
    )

    class Status:
        async def snapshot(self):
            return SimpleNamespace(profiles=(transient,))

    inspector = SimpleNamespace(
        observe=lambda: SimpleNamespace(owner=None, pid=None, inconsistent=False)
    )
    assert await _await_systemd_profile_ready(
        Status(), inspector, profile, 0, poll_interval=0
    ) is False


@pytest.mark.asyncio
async def test_systemd_readiness_forces_fresh_status_snapshot():
    profile = SimpleNamespace(id=SimpleNamespace(value="minecraft-sunlit-cobblemon"))
    status = SimpleNamespace(
        profile_id=profile.id,
        health="healthy",
        pid=101,
        required_ports_ready=True,
    )
    calls = []

    class Status:
        async def snapshot(self, *, force=False):
            calls.append(force)
            return SimpleNamespace(profiles=(status,))

    inspector = SimpleNamespace(
        observe=lambda: SimpleNamespace(owner=profile.id, pid=101, inconsistent=False)
    )
    assert await _await_systemd_profile_ready(
        Status(), inspector, profile, 1, poll_interval=0
    ) is True
    assert calls == [True]


class _Writer:
    def __init__(self, error: Exception):
        self.error = error
        self.closed = False
        self.wait_closed_called = False

    def get_extra_info(self, name: str):
        return object() if name == "socket" else None

    def write(self, _data: bytes) -> None:
        return None

    async def drain(self) -> None:
        raise self.error

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


class _FrameReader:
    def __init__(self, data: bytes | Exception):
        self.data = data

    async def readuntil(self, _separator: bytes) -> bytes:
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class _RecordingWriter:
    def __init__(self, *, socket_info: object | None = object(), wait_error: Exception | None = None):
        self.socket_info = socket_info
        self.wait_error = wait_error
        self.data: list[bytes] = []
        self.closed = False

    def get_extra_info(self, name: str):
        return self.socket_info if name == "socket" else None

    def write(self, data: bytes) -> None:
        self.data.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.wait_error is not None:
            raise self.wait_error


class _FakeAsyncServer:
    def __init__(self):
        self.closed = False
        self.wait_closed_called = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


def _server_for_client() -> UnixRpcServer:
    server = UnixRpcServer(object(), uid=1000, gid=2000, primary_gid=1000)
    server.peer_credentials = lambda _sock: (1234, 1000, 1000)
    return server


@pytest.mark.asyncio
async def test_non_generational_response_projects_at_current_watch_generation() -> None:
    server = UnixRpcServer(object(), uid=1000, gid=2000, primary_gid=1000)
    client = await server.watch_hub.subscribe()
    await server.watch_hub.publish("status", {}, generation=4, full=True)

    response = RpcSuccess(
        request_id=uuid4(),
        result=PerfSnapshot(
            cycle=PerfAggregate(count=1),
            rpc=PerfAggregate(count=1),
        ),
    )
    await server._publish_response("get_perf", response)

    assert (await client.queue.get()).kind == "status"
    observed = await client.queue.get()
    assert observed.kind == "get_perf"
    assert observed.generation == 4
    assert server.watch_hub.generation == 4


@pytest.mark.asyncio
async def test_explicit_stale_response_generation_is_still_rejected() -> None:
    server = UnixRpcServer(object(), uid=1000, gid=2000, primary_gid=1000)
    await server.watch_hub.publish("status", {}, generation=4, full=True)
    response = RpcSuccess(
        request_id=uuid4(),
        result=StatusSnapshot(
            generation=3,
            observed_at="2026-01-01T00:00:00Z",
            profiles=(),
        ),
    )

    with pytest.raises(WatchProtocolError, match="stale generation"):
        await server._publish_response("get_status", response)
    assert server.watch_hub.generation == 4


@pytest.mark.asyncio
async def test_status_snapshot_uses_browser_status_event_name() -> None:
    server = UnixRpcServer(object(), uid=1000, gid=2000, primary_gid=1000)
    client = await server.watch_hub.subscribe()
    response = RpcSuccess(
        request_id=uuid4(),
        result=StatusSnapshot(generation=1, observed_at="2026-01-01T00:00:00Z", profiles=()),
    )

    await server._publish_response("get_status", response)

    event = await client.queue.get()
    assert event.kind == "status"
    assert event.full is True


@pytest.mark.asyncio
async def test_non_generational_response_still_writes_rpc_reply_after_watch_projection() -> None:
    request = RpcRequest(request_id=uuid4(), actor="operator", action=GetPerf(kind="get_perf"))
    response = RpcSuccess(
        request_id=request.request_id,
        result=PerfSnapshot(
            cycle=PerfAggregate(count=1),
            rpc=PerfAggregate(count=1),
        ),
    )

    class Controller:
        async def execute(self, _request):
            return response

    server = UnixRpcServer(Controller(), uid=1000, gid=2000, primary_gid=1000)
    server.peer_credentials = lambda _sock: (1234, 1000, 1000)
    await server.watch_hub.publish("status", {}, generation=4, full=True)
    writer = _RecordingWriter()
    await server.handle_client(_Reader(request), writer)

    assert writer.closed is True
    assert len(writer.data) == 1
    assert b'"ok":true' in writer.data[0]
    assert server.watch_hub.generation == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")])
async def test_rpc_disconnected_write_warns_once_and_cleans_up(caplog, error: Exception) -> None:
    request = RpcRequest(
        request_id=uuid4(),
        actor="operator",
        action=ConfirmSwitch(kind="confirm_switch", confirmation_id=str(uuid4())),
    )
    writer = _Writer(error)

    class Controller:
        async def execute(self, _request):
            return failure(request.request_id, ErrorCode.INTERNAL_ERROR, "test")

    server = UnixRpcServer(Controller(), uid=1000, gid=2000, primary_gid=1000)
    server.peer_credentials = lambda _sock: (1234, 1000, 1000)

    with caplog.at_level(logging.WARNING, logger="game_control.slotd_main"):
        await server.handle_client(_Reader(request), writer)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "confirm_switch" in warnings[0].message
    assert "operator" in warnings[0].message
    assert writer.closed is True
    assert writer.wait_closed_called is True


@pytest.mark.parametrize("path_kind", ["symlink", "regular_file"])
def test_control_socket_path_rejects_symlink_and_non_socket(tmp_path, path_kind: str) -> None:
    socket_path = tmp_path / "control.sock"
    if path_kind == "symlink":
        target = tmp_path / "target"
        target.write_text("not a socket")
        socket_path.symlink_to(target)
    else:
        socket_path.write_text("not a socket")

    server = UnixRpcServer(object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000)
    with pytest.raises(PermissionError):
        server._check_existing_socket()


@pytest.mark.parametrize(
    ("parent_kind", "message"),
    [("symlink", "directory symlink"), ("regular_file", "parent is not a directory")],
)
def test_control_socket_parent_is_closed(tmp_path, parent_kind: str, message: str) -> None:
    parent = tmp_path / "parent"
    if parent_kind == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        parent.symlink_to(target, target_is_directory=True)
    else:
        parent.write_text("not a directory")

    server = UnixRpcServer(
        object(), socket_path=parent / "control.sock", uid=1000, gid=2000, primary_gid=1000
    )
    with pytest.raises(PermissionError, match=message):
        server._check_existing_socket()


@pytest.mark.asyncio
async def test_start_cleans_up_when_socket_permissions_cannot_be_set(tmp_path, monkeypatch) -> None:
    socket_path = tmp_path / "control.sock"
    fake_server = _FakeAsyncServer()

    async def fake_start_unix_server(*_args, **_kwargs):
        return fake_server

    monkeypatch.setattr(slotd_main.asyncio, "start_unix_server", fake_start_unix_server)

    def fail_chown(*_args):
        raise OSError("permission denied")

    monkeypatch.setattr(slotd_main.os, "chown", fail_chown)
    server = UnixRpcServer(object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000)

    with pytest.raises(PermissionError, match="unable to secure control socket"):
        await server.start()

    assert fake_server.closed is True
    assert fake_server.wait_closed_called is True


@pytest.mark.asyncio
async def test_start_rejects_insecure_socket_mode_and_accepts_secure_metadata(tmp_path, monkeypatch):
    socket_path = tmp_path / "control.sock"
    fake_server = _FakeAsyncServer()

    async def fake_start_unix_server(*_args, **_kwargs):
        return fake_server

    monkeypatch.setattr(slotd_main.asyncio, "start_unix_server", fake_start_unix_server)
    monkeypatch.setattr(slotd_main.os, "chown", lambda *_args: None)
    monkeypatch.setattr(slotd_main.os, "chmod", lambda *_args: None)
    original_stat = slotd_main.Path.stat

    def fake_stat(path, *args, **kwargs):
        if path == socket_path:
            return SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o600,
                st_uid=0,
                st_gid=2000,
                st_ino=42,
            )
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(slotd_main.Path, "stat", fake_stat)
    server = UnixRpcServer(object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000)

    with pytest.raises(PermissionError, match="insecure control socket ownership"):
        await server.start()

    assert fake_server.closed is True

    fake_server = _FakeAsyncServer()
    monkeypatch.setattr(slotd_main.asyncio, "start_unix_server", lambda *_args, **_kwargs: fake_server)

    async def fake_start_unix_server_secure(*_args, **_kwargs):
        return fake_server

    monkeypatch.setattr(slotd_main.asyncio, "start_unix_server", fake_start_unix_server_secure)
    monkeypatch.setattr(
        slotd_main.Path,
        "stat",
        lambda path, *args, **kwargs: SimpleNamespace(
            st_mode=stat.S_IFSOCK | 0o660,
            st_uid=0,
            st_gid=2000,
            st_ino=43,
        )
        if path == socket_path
        else original_stat(path, *args, **kwargs),
    )
    server = UnixRpcServer(object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000)
    await server.start()
    assert server._bound_inode == 43


def test_start_refuses_a_live_control_socket(tmp_path) -> None:
    socket_path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen()
    try:
        server = UnixRpcServer(
            object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000
        )
        with pytest.raises(RuntimeError, match="control socket is live"):
            asyncio.run(server.start())
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_close_stops_server_and_unlinks_only_bound_socket(tmp_path) -> None:
    socket_path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    fake_server = _FakeAsyncServer()
    server = UnixRpcServer(object(), socket_path=socket_path, uid=1000, gid=2000, primary_gid=1000)
    server._server = fake_server
    server._bound_inode = socket_path.stat().st_ino

    await server.close()

    listener.close()
    assert fake_server.closed is True
    assert fake_server.wait_closed_called is True
    assert not socket_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [b"not-json\n", b'{"actor":"operator"}\n', b"x" * (MAX_REQUEST_BYTES + 1) + b"\n"],
)
async def test_rpc_rejects_malformed_and_oversized_frames(frame: bytes) -> None:
    server = _server_for_client()
    writer = _RecordingWriter()

    await server.handle_client(_FrameReader(frame), writer)

    response = response_from_json(writer.data[0])
    assert isinstance(response, slotd_main.RpcFailure)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert writer.closed is True


@pytest.mark.asyncio
async def test_rpc_rejects_incomplete_frame_and_closes_peer() -> None:
    server = _server_for_client()
    writer = _RecordingWriter(wait_error=ConnectionResetError("peer vanished"))
    reader_error = asyncio.IncompleteReadError(partial=b"{", expected=20)

    await server.handle_client(_FrameReader(reader_error), writer)

    response = response_from_json(writer.data[0])
    assert isinstance(response, slotd_main.RpcFailure)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert writer.closed is True


@pytest.mark.asyncio
async def test_rpc_rejects_unauthorized_peer_before_reading_request() -> None:
    server = _server_for_client()
    server.peer_credentials = lambda _sock: (1234, 4000, 4000)
    writer = _RecordingWriter()

    await server.handle_client(_FrameReader(b"not-read\n"), writer)

    response = response_from_json(writer.data[0])
    assert isinstance(response, slotd_main.RpcFailure)
    assert response.error.code is ErrorCode.UNAUTHORIZED_PEER
    assert writer.closed is True


@pytest.mark.asyncio
async def test_await_free_slot_times_out_when_lock_state_never_releases() -> None:
    class Inspector:
        def observe(self):
            return SlotObservation(owner="minecraft", inconsistent=True)

    assert await _await_free_slot(Inspector(), 0, poll_interval=0) is False


@pytest.mark.asyncio
async def test_serve_cleans_up_after_startup_lock_failure_and_signal_shutdown(monkeypatch) -> None:
    class Services:
        tps_sampler = None

    class Controller:
        services = Services()

        async def reconcile_startup(self):
            raise BlockingIOError("operation lock unavailable")

    class Server:
        def __init__(self, _controller):
            self._server = self
            self.closed = False

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(3600)

        async def close(self):
            self.closed = True

    server = None

    def make_server(controller):
        nonlocal server
        server = Server(controller)
        return server

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", make_server)

    with pytest.raises(BlockingIOError, match="operation lock unavailable"):
        await slotd_main.serve()

    assert server is not None
    assert server.closed is True


@pytest.mark.asyncio
async def test_serve_cancels_tps_sampler_and_closes_it_on_shutdown(monkeypatch) -> None:
    class Sampler:
        def __init__(self):
            self.cancelled = False
            self.closed = False

        async def run(self, _minecraft_running):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def aclose(self):
            self.closed = True

    sampler = Sampler()
    close_calls = []

    class Services:
        tps_sampler = sampler

        async def close(self):
            close_calls.append("closed")

    class Controller:
        services = Services()
        slot_inspector = None
        profiles = ()
        adapters = {}

        async def reconcile_startup(self):
            return None

    class Server:
        def __init__(self, _controller):
            self._server = self

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        async def close(self):
            return None

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", Server)

    with pytest.raises(asyncio.CancelledError):
        await slotd_main.serve()

    assert sampler.cancelled is True
    assert sampler.closed is True
    assert close_calls == ["closed"]


@pytest.mark.asyncio
async def test_serve_closes_services_and_server_when_tps_close_fails(monkeypatch) -> None:
    close_calls = []

    class Sampler:
        async def run(self, _minecraft_running):
            await asyncio.sleep(3600)

        async def aclose(self):
            raise RuntimeError("tps close failed")

    sampler = Sampler()

    class Services:
        tps_sampler = sampler

        async def close(self):
            close_calls.append("services")

    class Controller:
        services = Services()
        slot_inspector = None
        profiles = ()
        adapters = {}

        async def reconcile_startup(self):
            return None

    class Server:
        def __init__(self, _controller):
            self._server = self
            self.closed = False

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        async def close(self):
            self.closed = True

    server = None

    def make_server(controller):
        nonlocal server
        server = Server(controller)
        return server

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", make_server)

    with pytest.raises(RuntimeError, match="tps close failed"):
        await slotd_main.serve()

    assert close_calls == ["services"]
    assert server is not None and server.closed is True


@pytest.mark.asyncio
async def test_serve_fails_when_maintenance_task_exits_unexpectedly(monkeypatch):
    close_calls = []

    class Services:
        async def close(self):
            close_calls.append("closed")

    class Controller:
        services = Services()

        async def reconcile_startup(self):
            return None

    class Server:
        def __init__(self, _controller):
            self._server = self
            self.closed = False

        async def start(self):
            return None

        async def serve_forever(self):
            await asyncio.sleep(3600)

        async def close(self):
            self.closed = True

    server = None

    def make_server(controller):
        nonlocal server
        server = Server(controller)
        return server

    async def exited_loop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", make_server)
    monkeypatch.setattr(slotd_main, "_maintenance_loop", exited_loop)

    with pytest.raises(RuntimeError, match="maintenance task exited unexpectedly"):
        await slotd_main.serve()
    assert server is not None and server.closed is True
    assert close_calls == ["closed"]


@pytest.mark.asyncio
async def test_serve_fails_when_rpc_server_exits_normally(monkeypatch):
    class Services:
        tps_sampler = None

    class Controller:
        services = Services()

        async def reconcile_startup(self):
            return None

    class Server:
        def __init__(self, _controller):
            self._server = self
            self.closed = False

        async def start(self):
            return None

        async def serve_forever(self):
            return None

        async def close(self):
            self.closed = True

    server = None

    def make_server(controller):
        nonlocal server
        server = Server(controller)
        return server

    async def running_loop(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(slotd_main, "build_controller_assembly", lambda: _test_assembly(Controller()))
    monkeypatch.setattr(slotd_main, "UnixRpcServer", make_server)
    monkeypatch.setattr(slotd_main, "_maintenance_loop", running_loop)

    with pytest.raises(RuntimeError, match="RPC server task exited unexpectedly"):
        await slotd_main.serve()
    assert server is not None and server.closed is True


@pytest.mark.asyncio
async def test_maintenance_loop_survives_tick_failure_and_cleans_up_on_cancel(caplog):
    ticks = []

    class Controller:
        from game_control.perf import PerformanceTracker
        performance = PerformanceTracker(maxlen=4)

        async def maintenance_tick(self):
            ticks.append("tick")
            if len(ticks) == 1:
                raise RuntimeError("transient maintenance failure")

    task = asyncio.create_task(_maintenance_loop(Controller(), interval_seconds=0.1))
    while len(ticks) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(ticks) >= 2
    assert "slotd maintenance tick failed" in caplog.text
    assert Controller.performance.snapshot()["maintenance"]["count"] >= 2
    assert all(value >= 0 for value in Controller.performance.snapshot()["maintenance_ms"])


@pytest.mark.asyncio
async def test_maintenance_loop_propagates_initialization_failure():
    async def fail_startup():
        raise RuntimeError("startup failed")

    initialization = asyncio.create_task(fail_startup())
    with pytest.raises(RuntimeError, match="startup failed"):
        await _maintenance_loop(SimpleNamespace(), initialization=initialization)


@pytest.mark.asyncio
async def test_maintenance_loop_keeps_semantics_when_recorder_is_missing_or_fails():
    class Controller:
        performance = SimpleNamespace(record_maintenance=lambda _value: (_ for _ in ()).throw(RuntimeError("recorder")))
        async def maintenance_tick(self):
            raise RuntimeError("tick")

    task = asyncio.create_task(_maintenance_loop(Controller(), interval_seconds=0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_maintenance_recorder_cancelled_error_never_masks_tick_or_outer_cancel():
    class FailingTick:
        class Performance:
            @staticmethod
            def record_maintenance(_value):
                raise asyncio.CancelledError
        performance = Performance()
        def __init__(self):
            self.ticks = 0
        async def maintenance_tick(self):
            self.ticks += 1
            raise RuntimeError("original maintenance failure")

    controller = FailingTick()
    task = asyncio.create_task(_maintenance_loop(controller, interval_seconds=0.01))
    while controller.ticks < 2:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    class Successful:
        class Performance:
            @staticmethod
            def record_maintenance(_value):
                raise asyncio.CancelledError
        performance = Performance()
        async def maintenance_tick(self):
            return None

    task = asyncio.create_task(_maintenance_loop(Successful(), interval_seconds=0.01))
    await asyncio.sleep(0.03)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    class Legacy:
        async def maintenance_tick(self):
            return None
    task = asyncio.create_task(_maintenance_loop(Legacy(), interval_seconds=0.01))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
