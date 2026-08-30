import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from game_control.adapters.base import AdapterError
from game_control.adapters.systemd import SystemdAdapter
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


def _profile(
    *, adapter: AdapterKind = AdapterKind.SYSTEMD, profile_id: ProfileId = ProfileId.TERRARIA_TMOD
) -> Profile:
    values = {
        "id": profile_id,
        "display_name": str(profile_id).title(),
        "adapter": adapter,
        "process": ProcessSpec(executable=Path("/usr/bin/game")),
        "ports": (PortSpec(protocol="tcp", port=7777),),
        "start_timeout_seconds": 30,
        "stop_timeout_seconds": 30,
        "health_timeout_seconds": 30,
        "paths": PathSpec(
            data_roots=(Path("/var/lib/game-control/game"),),
            mutable_root=Path("/var/lib/game-control/game"),
            backup_root=Path("/var/backups/game-control/game"),
            install_root=Path("/opt/game-control/game"),
            version_file=Path("/var/lib/game-control/game/version"),
        ),
        "min_available_memory_bytes": 1,
        "min_free_disk_bytes": 1,
        "operations": frozenset({OperationName.START}),
        "update": UpdateSpec(kind="manual"),
    }
    if adapter is AdapterKind.SYSTEMD:
        values["systemd_unit"] = f"{profile_id}.service"
    else:
        values["crafty_server_id"] = uuid4()
    return Profile(**values)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("start", ("/usr/bin/systemctl", "start", "terraria-tmod.service")),
        ("stop", ("/usr/bin/systemctl", "stop", "terraria-tmod.service")),
        (
            "kill",
            ("/usr/bin/systemctl", "kill", "-s", "SIGKILL", "terraria-tmod.service"),
        ),
        (
            "force_stop",
            ("/usr/bin/systemctl", "kill", "-s", "SIGKILL", "terraria-tmod.service"),
        ),
        (
            "reset_failed",
            ("/usr/bin/systemctl", "reset-failed", "terraria-tmod.service"),
        ),
        (
            "settle_state",
            (
                "/usr/bin/systemctl",
                "show",
                "terraria-tmod.service",
                "--property=ActiveState,SubState,Job",
            ),
        ),
        (
            "observe",
            (
                "/usr/bin/systemctl",
                "show",
                "terraria-tmod.service",
                "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic",
            ),
        ),
        (
            "logs",
            (
                "/usr/bin/journalctl",
                "--namespace=horizon",
                "-u",
                "terraria-tmod.service",
                "--no-pager",
                "-o",
                "json",
            ),
        ),
    ],
)
def test_command_covers_supported_systemd_operations(operation, expected) -> None:
    assert SystemdAdapter().command(operation, _profile()) == expected


def test_command_rejects_unknown_operation() -> None:
    with pytest.raises(KeyError, match="unsupported"):
        SystemdAdapter().command("reload", _profile())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "message"),
    [
        ("start", "systemd start failed"),
        ("graceful_stop", "systemd stop failed"),
        ("force_stop", "systemd force stop failed"),
    ],
)
async def test_lifecycle_nonzero_exit_is_a_nonretryable_adapter_error(method, message) -> None:
    adapter = SystemdAdapter()

    async def run(_argv, **_kwargs):
        return 23, b"", b"unit failure"

    adapter._run = run
    with pytest.raises(AdapterError, match=message) as caught:
        await getattr(adapter, method)(_profile())
    assert caught.value.retryable is False
    assert caught.value.returncode == 23


@pytest.mark.asyncio
async def test_stop_delegates_to_graceful_stop() -> None:
    adapter = SystemdAdapter()
    calls = []

    async def graceful(profile):
        calls.append(profile)

    adapter.graceful_stop = graceful
    profile = _profile()
    await adapter.stop(profile)
    assert calls == [profile]


@pytest.mark.asyncio
async def test_force_stop_waits_for_inactive_and_resets_failed_state() -> None:
    adapter = SystemdAdapter()
    calls = []
    states = iter(
        (
            b"ActiveState=deactivating\nSubState=stop-post\nJob=123 stop\n",
            b"ActiveState=failed\nSubState=failed\nJob=\n",
            b"ActiveState=inactive\nSubState=dead\nJob=\n",
        )
    )

    async def run(argv, **_kwargs):
        calls.append(argv)
        if "--property=ActiveState,SubState,Job" in argv:
            return 0, next(states), b""
        return 0, b"", b""

    adapter._run = run
    await adapter.force_stop(_profile())

    assert calls == [
        ("/usr/bin/systemctl", "kill", "-s", "SIGKILL", "terraria-tmod.service"),
        (
            "/usr/bin/systemctl", "show", "terraria-tmod.service",
            "--property=ActiveState,SubState,Job",
        ),
        (
            "/usr/bin/systemctl", "show", "terraria-tmod.service",
            "--property=ActiveState,SubState,Job",
        ),
        ("/usr/bin/systemctl", "reset-failed", "terraria-tmod.service"),
        (
            "/usr/bin/systemctl", "show", "terraria-tmod.service",
            "--property=ActiveState,SubState,Job",
        ),
    ]


@pytest.mark.asyncio
async def test_force_stop_rejects_reset_that_does_not_leave_unit_inactive() -> None:
    adapter = SystemdAdapter()

    async def run(argv, **_kwargs):
        if "--property=ActiveState,SubState,Job" in argv:
            return 0, b"ActiveState=failed\nSubState=failed\nJob=\n", b""
        return 0, b"", b""

    adapter._run = run
    with pytest.raises(AdapterError, match="cleanup did not settle"):
        await adapter.force_stop(_profile())


@pytest.mark.asyncio
async def test_send_command_writes_only_to_the_fixed_console_helper() -> None:
    adapter = SystemdAdapter()
    seen = {}

    async def run(argv, *, timeout, input_data):
        seen.update(argv=argv, timeout=timeout, input_data=input_data)
        return 0, b"", b""

    adapter._run = run
    await adapter.send_command(_profile(), "save-all")
    assert seen == {
        "argv": ("/usr/local/libexec/game-console-command", "terraria-tmod"),
        "timeout": 10.0,
        "input_data": b"save-all",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["", " save", "save ", "save\n", "\x7f", "x" * 513])
async def test_send_command_rejects_unsafe_console_input_before_spawn(command) -> None:
    adapter = SystemdAdapter()

    async def run(*_args, **_kwargs):
        raise AssertionError("unsafe command reached the process boundary")

    adapter._run = run
    with pytest.raises(AdapterError, match="unsupported"):
        await adapter.send_command(_profile(), command)


@pytest.mark.asyncio
async def test_send_command_rejects_non_console_and_non_systemd_profiles() -> None:
    adapter = SystemdAdapter()

    async def run(*_args, **_kwargs):
        raise AssertionError("unsupported profile reached the process boundary")

    adapter._run = run
    with pytest.raises(AdapterError, match="unsupported"):
        await adapter.send_command(_profile(profile_id=ProfileId.MINECRAFT), "list")
    with pytest.raises(AdapterError, match="unsupported"):
        await adapter.send_command(_profile(adapter=AdapterKind.CRAFTY), "list")


@pytest.mark.asyncio
async def test_send_command_maps_helper_failure_as_retryable() -> None:
    adapter = SystemdAdapter()

    async def run(*_args, **_kwargs):
        return 75, b"", b"helper unavailable"

    adapter._run = run
    with pytest.raises(AdapterError, match="systemd command failed") as caught:
        await adapter.send_command(_profile(), "list")
    assert caught.value.retryable is True
    assert caught.value.returncode == 75


@pytest.mark.asyncio
async def test_send_command_rejects_unencodable_surrogate() -> None:
    with pytest.raises(AdapterError, match="unsupported"):
        await SystemdAdapter().send_command(_profile(), "bad\ud800")


class _FakeProcess:
    def __init__(self, *, returncode=0, communicate_result=(b"", b"")):
        self.returncode = returncode
        self.stdout = None
        self.stderr = None
        self.communicate_result = communicate_result
        self.received_input = None
        self.killed = False
        self.waited = False

    async def communicate(self, input=None):
        self.received_input = input
        return self.communicate_result

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


@pytest.mark.asyncio
async def test_run_uses_fake_communicate_and_preserves_returncode(monkeypatch) -> None:
    process = _FakeProcess(returncode=4, communicate_result=(b"out", None))

    async def create(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    result = await SystemdAdapter()._run(("/usr/bin/systemctl", "start", "x.service"), input_data=b"x")
    assert result == (4, b"out", b"")
    assert process.received_input == b"x"


@pytest.mark.asyncio
async def test_run_converts_spawn_oserror_to_redacted_adapter_error(monkeypatch) -> None:
    async def create(*_argv, **_kwargs):
        raise FileNotFoundError("do not expose this path")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(AdapterError, match="systemd command failed") as caught:
        await SystemdAdapter()._run(("/usr/bin/systemctl", "start", "x.service"))
    assert "do not expose" not in str(caught.value)


@pytest.mark.asyncio
async def test_run_timeout_kills_and_awaits_fake_process(monkeypatch) -> None:
    process = _FakeProcess()

    async def create(*_argv, **_kwargs):
        return process

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(1)
        return b"", b""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr("game_control.adapters.systemd._bounded_communicate", hang)
    with pytest.raises(AdapterError, match="timed out"):
        await SystemdAdapter()._run(("/usr/bin/systemctl", "start", "x.service"), timeout=0.001)
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_run_cancellation_kills_and_reraises(monkeypatch) -> None:
    process = _FakeProcess()

    async def create(*_argv, **_kwargs):
        return process

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr("game_control.adapters.systemd._bounded_communicate", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await SystemdAdapter()._run(("/usr/bin/systemctl", "start", "x.service"))
    assert process.killed is True
    assert process.waited is True


@pytest.mark.asyncio
async def test_run_rejects_oversized_input_before_spawn(monkeypatch) -> None:
    async def create(*_argv, **_kwargs):
        raise AssertionError("oversized input reached process creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(AdapterError, match="input exceeded limit"):
        await SystemdAdapter()._run(("/usr/bin/game-console-command", "x"), input_data=b"x" * 4097)
