from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from game_control.adapters.crafty import CraftyAdapter
from game_control.adapters.systemd import SystemdAdapter
from game_control.adapters.base import AdapterError
from game_control.models import (
    AdapterKind,
    PathSpec,
    PortSpec,
    ProcessSpec,
    Profile,
    ProfileId,
    OperationName,
    UpdateSpec,
)


def _profile(*, adapter: AdapterKind) -> Profile:
    kwargs = {
        "id": ProfileId.MINECRAFT,
        "display_name": "Minecraft",
        "adapter": adapter,
        "process": ProcessSpec(executable=Path("/usr/bin/java")),
        "ports": (PortSpec(protocol="tcp", port=25565),),
        "start_timeout_seconds": 30,
        "stop_timeout_seconds": 30,
        "health_timeout_seconds": 30,
        "paths": PathSpec(
            data_roots=(Path("/var/lib/game-control/minecraft"),),
            mutable_root=Path("/var/lib/game-control/minecraft"),
            backup_root=Path("/var/backups/game-control/minecraft"),
            install_root=Path("/opt/game-control/minecraft"),
            version_file=Path("/var/lib/game-control/minecraft/version"),
        ),
        "min_available_memory_bytes": 1,
        "min_free_disk_bytes": 1,
        "operations": frozenset({OperationName.START}),
        "update": UpdateSpec(kind="manual"),
    }
    if adapter is AdapterKind.CRAFTY:
        kwargs["crafty_server_id"] = uuid4()
    else:
        kwargs["systemd_unit"] = "minecraft.service"
    return Profile(**kwargs)


def test_crafty_uses_only_fixed_v2_routes() -> None:
    adapter = CraftyAdapter("https://crafty.invalid", "token")
    profile = _profile(adapter=AdapterKind.CRAFTY)
    assert adapter.route("stats", profile) == (
        "GET",
        f"/api/v2/servers/{profile.crafty_server_id}/stats",
    )
    with pytest.raises(KeyError):
        adapter.route("arbitrary", profile)


@pytest.mark.asyncio
async def test_crafty_send_command_posts_raw_stdin_to_fixed_route() -> None:
    calls = []

    class Response:
        def raise_for_status(self): pass

    class Client:
        async def request(self, *args, **kwargs):
            calls.append((args, kwargs))
            return Response()

    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    profile = _profile(adapter=AdapterKind.CRAFTY)
    await adapter.send_command(profile, "list")
    assert calls == [
        (("POST", f"/api/v2/servers/{profile.crafty_server_id}/stdin/"), {"content": b"list"})
    ]


@pytest.mark.asyncio
async def test_crafty_send_command_rejects_invalid_before_network() -> None:
    class Client:
        async def request(self, *_args, **_kwargs):
            raise AssertionError("invalid command reached Crafty")

    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    with pytest.raises(AdapterError):
        await adapter.send_command(_profile(adapter=AdapterKind.CRAFTY), " list")


def test_systemd_argv_is_fixed_and_unit_comes_from_profile() -> None:
    adapter = SystemdAdapter()
    argv = adapter.command("start", _profile(adapter=AdapterKind.SYSTEMD))
    assert argv == ("/usr/bin/systemctl", "start", "minecraft.service")
    assert all(value not in argv for value in ("sh", "bash"))


@pytest.mark.asyncio
async def test_systemd_observe_parses_named_show_properties() -> None:
    adapter = SystemdAdapter()

    async def run(_argv, **_kwargs):
        return (
            0,
            b"MainPID=1234\nExecMainStartTimestamp=Sat 2026-07-11 12:34:43 UTC\n"
            b"ExecMainStartTimestampMonotonic=205240985153\nActiveState=active\nSubState=running\n",
            b"",
        )

    adapter._run = run
    observed = await adapter.observe(_profile(adapter=AdapterKind.SYSTEMD))
    assert observed.running is True
    assert observed.healthy is True
    assert observed.pid == 1234


@pytest.mark.asyncio
async def test_crafty_stream_response_is_bounded() -> None:
    class Response:
        def raise_for_status(self): pass
        async def aiter_bytes(self):
            yield b"x" * (256 * 1024 + 1)
    class Stream:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *_): return False
    class Client:
        def stream(self, *_args): return Stream()
    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    with pytest.raises(AdapterError, match="exceeded limit"):
        await adapter.observe(_profile(adapter=AdapterKind.CRAFTY))


@pytest.mark.asyncio
async def test_systemd_stop_timeout_uses_profile_budget() -> None:
    adapter = SystemdAdapter()
    seen = {}
    async def run(argv, *, timeout=0):
        seen["timeout"] = timeout
        return 0, b"", b""
    adapter._run = run
    profile = _profile(adapter=AdapterKind.SYSTEMD).model_copy(update={"stop_timeout_seconds": 90})
    await adapter.graceful_stop(profile)
    assert seen["timeout"] == 95


@pytest.mark.asyncio
async def test_crafty_observe_unwraps_data_and_ping_health() -> None:
    class Response:
        content = b'{"data":{"running":true,"crashed":false,"int_ping_results":{"tcp":true}}}'
        def raise_for_status(self): pass
    class Client:
        async def request(self, *_args):
            return Response()
    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    observation = await adapter.observe(_profile(adapter=AdapterKind.CRAFTY))
    assert observation.running is True and observation.healthy is True


@pytest.mark.asyncio
async def test_crafty_running_without_health_signal_is_unknown() -> None:
    class Response:
        content = b'{"data":{"running":true}}'
        def raise_for_status(self): pass
    class Client:
        async def request(self, *_args):
            return Response()
    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    assert (await adapter.observe(_profile(adapter=AdapterKind.CRAFTY))).healthy is None


@pytest.mark.asyncio
async def test_crafty_empty_ping_results_are_unknown() -> None:
    class Response:
        content = b'{"data":{"running":true,"crashed":false,"int_ping_results":{}}}'
        def raise_for_status(self): pass
    class Client:
        async def request(self, *_args):
            return Response()
    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    assert (await adapter.observe(_profile(adapter=AdapterKind.CRAFTY))).healthy is None


@pytest.mark.asyncio
async def test_systemd_journal_lines_have_aware_timestamps() -> None:
    adapter = SystemdAdapter()
    async def run(argv, *, timeout=0):
        return 0, b'{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"ready","PRIORITY":"6"}\n', b""
    adapter._run = run
    lines = await adapter.recent_logs(_profile(adapter=AdapterKind.SYSTEMD), 5)
    assert lines[0].timestamp.tzinfo is not None
    command = adapter.command("logs", _profile(adapter=AdapterKind.SYSTEMD))
    assert command[command.index("-o") + 1] == "json"


@pytest.mark.asyncio
async def test_systemd_logs_include_aware_time_range_in_argv() -> None:
    adapter = SystemdAdapter()
    seen = []

    async def run(argv, *, timeout=0):
        seen.append(argv)
        return 0, b"", b""

    adapter._run = run
    await adapter.recent_logs(
        _profile(adapter=AdapterKind.SYSTEMD),
        5,
        since=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        until=datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc),
    )
    assert seen and seen[0][-6:] == (
        "--since", "2026-07-13 12:00:00", "--until", "2026-07-13 13:00:00", "-n", "5"
    )


@pytest.mark.asyncio
async def test_crafty_structured_log_timestamp_primitives_are_normalized() -> None:
    class Response:
        content = (
            b'{"logs":['
            b'{"timestamp":"2025-01-01T00:00:00Z","message":"iso"},'
            b'{"timestamp":1735689600,"message":"epoch"},'
            b'{"timestamp":"2025-01-01T00:00:00","message":"naive"}]}'
        )
        def raise_for_status(self): pass

    class Client:
        async def request(self, *_args):
            return Response()

    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    lines = await adapter.recent_logs(_profile(adapter=AdapterKind.CRAFTY), 5)
    assert [line.message for line in lines] == ["iso", "epoch"]
    assert all(line.timestamp.tzinfo is not None for line in lines)


@pytest.mark.asyncio
async def test_systemd_priority_range_maps_to_severity() -> None:
    adapter = SystemdAdapter()
    async def run(argv, *, timeout=0):
        payload = b"\n".join(
            f'{{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"p{priority}","PRIORITY":"{priority}"}}'.encode()
            for priority in range(8)
        ) + b"\n"
        return 0, payload, b""
    adapter._run = run
    lines = await adapter.recent_logs(_profile(adapter=AdapterKind.SYSTEMD), 10)
    assert [line.severity for line in lines] == ["error", "error", "error", "error", "warning", "info", "info", "debug"]


@pytest.mark.asyncio
async def test_systemd_large_journal_page_is_bounded_by_records() -> None:
    adapter = SystemdAdapter()
    async def run(argv, *, timeout=0):
        payload = b"\n".join(
            b'{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"line","PRIORITY":"6"}'
            for _ in range(10000)
        ) + b"\n"
        return 0, payload, b""
    adapter._run = run
    lines = await adapter.recent_logs(_profile(adapter=AdapterKind.SYSTEMD), 3)
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_systemd_journal_discards_oversized_record_and_continues() -> None:
    import game_control.adapters.systemd as systemd_module

    class Stream:
        def __init__(self, values):
            self.values = iter(values)

        async def readline(self):
            return next(self.values, b"")

        async def read(self, _limit):
            return b""

    class Process:
        def __init__(self):
            self.stdout = Stream(
                [
                    b"x" * (systemd_module._MAX_JOURNAL_RECORD + 1) + b"\n",
                    b'{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"kept","PRIORITY":"6"}\n',
                ]
            )
            self.stderr = Stream([])
            self.waited = False

        async def wait(self):
            self.waited = True

    process = Process()
    stdout, _ = await systemd_module._stream_journal(process, 10)
    assert b"kept" in stdout
    assert b"x" * 128 not in stdout
    assert process.waited is True


@pytest.mark.asyncio
async def test_systemd_real_stream_reader_overflow_continues() -> None:
    import asyncio
    import game_control.adapters.systemd as systemd_module

    stdout = asyncio.StreamReader(limit=128)
    stdout.feed_data(
        b"x" * 256
        + b"\n"
        + b'{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"kept","PRIORITY":"6"}\n'
    )
    stdout.feed_eof()
    stderr = asyncio.StreamReader(limit=16)
    stderr.feed_eof()
    process = type("Process", (), {"stdout": stdout, "stderr": stderr, "wait": lambda self: _wait()})()
    output, _ = await systemd_module._stream_journal(process, 10)
    assert b'"MESSAGE":"kept"' in output


@pytest.mark.asyncio
async def test_systemd_journal_error_kills_and_awaits_process(monkeypatch) -> None:
    import asyncio
    import game_control.adapters.systemd as systemd_module

    process = type(
        "Process",
        (),
        {
            "returncode": None,
            "killed": False,
            "waited": False,
            "kill": lambda self: setattr(self, "killed", True),
        },
    )()

    async def wait(self):
        self.waited = True

    process.wait = wait.__get__(process)

    seen = {}

    async def create(*argv, **kwargs):
        seen.update(kwargs)
        return process

    async def fail(*_args, **_kwargs):
        raise ValueError("invalid journal stream")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(systemd_module, "_stream_journal", fail)
    adapter = SystemdAdapter()
    with pytest.raises(AdapterError):
        await adapter._run(adapter.command("logs", _profile(adapter=AdapterKind.SYSTEMD)))
    assert seen["limit"] > systemd_module._MAX_JOURNAL_RECORD
    assert process.killed is True
    assert process.waited is True


async def _wait():
    return None
