import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import game_control.adapters.systemd as systemd_module
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


def _profile(profile_id: ProfileId = ProfileId.TERRARIA_TMOD) -> Profile:
    return Profile(
        id=profile_id,
        display_name=profile_id.value,
        adapter=AdapterKind.SYSTEMD,
        systemd_unit=f"{profile_id.value}.service",
        process=ProcessSpec(executable=Path("/usr/bin/game")),
        ports=(PortSpec(protocol="tcp", port=7777),),
        start_timeout_seconds=30,
        stop_timeout_seconds=30,
        health_timeout_seconds=30,
        paths=PathSpec(
            data_roots=(Path("/var/lib/game-control/game"),),
            mutable_root=Path("/var/lib/game-control/game"),
            backup_root=Path("/var/backups/game-control/game"),
            install_root=Path("/opt/game-control/game"),
            version_file=Path("/var/lib/game-control/game/version"),
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.START}),
        update=UpdateSpec(kind="manual"),
    )


async def _observe_from(payload: bytes):
    adapter = SystemdAdapter()

    async def run(_argv, **_kwargs):
        return 0, payload, b""

    adapter._run = run
    return await adapter.observe(_profile())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "substate"),
    [
        ("activating", "start"),
        ("deactivating", "stop-sigterm"),
        ("failed", "failed"),
        ("inactive", "dead"),
        ("not-found", "dead"),
    ],
)
async def test_observe_maps_nonready_systemd_unit_states_to_unhealthy(active, substate) -> None:
    observation = await _observe_from(
        f"ActiveState={active}\nSubState={substate}\nMainPID=987\n".encode()
    )
    assert observation.running is False
    assert observation.healthy is False
    assert observation.pid == 987


@pytest.mark.asyncio
@pytest.mark.parametrize("substate", ["running", "listening", "exited"])
async def test_observe_maps_supported_active_substates_to_healthy(substate) -> None:
    observation = await _observe_from(
        f"ActiveState=active\nSubState={substate}\nMainPID=987\n".encode()
    )
    assert observation.running is True
    assert observation.healthy is True


@pytest.mark.asyncio
async def test_observe_handles_command_failure_as_not_running() -> None:
    adapter = SystemdAdapter()

    async def run(_argv, **_kwargs):
        return 3, b"", b"systemctl failed"

    adapter._run = run
    observation = await adapter.observe(_profile())
    assert observation.running is False
    assert observation.healthy is False
    assert observation.pid is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_count", "expected_names"),
    [
        ("There are 0 of a max of 20 players online:", 0, ()),
        ("There are 2 of a max of 20 players online: Player_one, PlayerTwo", 2, ("Player_one", "PlayerTwo")),
    ],
)
async def test_sunlit_observe_uses_one_fixed_rcon_list_for_count_and_names(
    response, expected_count, expected_names
) -> None:
    commands = []

    class _Rcon:
        async def execute(self, command):
            commands.append(command)
            return response

    adapter = SystemdAdapter(rcon=_Rcon())

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=active\nSubState=running\nMainPID=987\n", b""

    adapter._run = run
    observation = await adapter.observe(_profile(ProfileId.MINECRAFT_SUNLIT_COBBLEMON))
    assert observation.players_online == expected_count
    assert observation.player_names == expected_names
    assert commands == ["list"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_count"),
    [
        ("There are 2 of a max of 20 players online: OnlyOne", 2),
        ("There are 2 of a max of 20 players online: Duplicate, Duplicate", 2),
        ("There are 1 of a max of 20 players online: invalid-name", 1),
    ],
)
async def test_sunlit_observe_keeps_valid_count_but_rejects_invalid_name_list(
    response, expected_count
) -> None:
    class _Rcon:
        async def execute(self, _command):
            return response

    adapter = SystemdAdapter(rcon=_Rcon())

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=active\nSubState=running\nMainPID=987\n", b""

    adapter._run = run
    observation = await adapter.observe(_profile(ProfileId.MINECRAFT_SUNLIT_COBBLEMON))
    assert observation.players_online == expected_count
    assert observation.player_names is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "",
        "unknown",
        "There are 21 of a max of 20 players online:",
        "There are 0 of a max of 10001 players online:",
        "There are -1 of a max of 20 players online:",
    ],
)
async def test_sunlit_observe_fails_closed_for_invalid_rcon_player_count(response) -> None:
    class _Rcon:
        async def execute(self, _command):
            return response

    adapter = SystemdAdapter(rcon=_Rcon())

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=active\nSubState=running\nMainPID=987\n", b""

    adapter._run = run
    observation = await adapter.observe(_profile(ProfileId.MINECRAFT_SUNLIT_COBBLEMON))
    assert observation.players_online is None
    assert observation.player_names is None


@pytest.mark.asyncio
async def test_sunlit_observe_fails_closed_when_rcon_list_fails() -> None:
    class _Rcon:
        async def execute(self, _command):
            raise OSError("RCON unavailable")

    adapter = SystemdAdapter(rcon=_Rcon())

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=active\nSubState=running\nMainPID=987\n", b""

    adapter._run = run
    observation = await adapter.observe(_profile(ProfileId.MINECRAFT_SUNLIT_COBBLEMON))
    assert observation.players_online is None


@pytest.mark.asyncio
async def test_non_sunlit_observe_never_calls_rcon() -> None:
    class _Rcon:
        async def execute(self, _command):
            raise AssertionError("non-Sunlit profile reached RCON")

    adapter = SystemdAdapter(rcon=_Rcon())

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=active\nSubState=running\nMainPID=987\n", b""

    adapter._run = run
    observation = await adapter.observe(_profile())
    assert observation.players_online is None


@pytest.mark.asyncio
async def test_observe_parses_hostile_properties_without_trusting_partial_values() -> None:
    observation = await _observe_from(
        b"garbage-without-separator\n"
        b"MainPID=not-a-pid\n"
        b"MainPID=42=extra\n"
        b"ExecMainStartTimestamp=not-a-timestamp\n"
        b"ExecMainStartTimestampMonotonic=not-a-number\n"
        b"ActiveState=active=unexpected\n"
        b"SubState=running\n"
        b"invalid utf8=\xff\n"
    )
    assert observation.running is False
    assert observation.healthy is False
    assert observation.pid is None
    assert observation.started_at is None
    assert observation.players_online is None
    assert observation.required_ports_ready is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"ActiveState=active\nSubState=running\nMainPID=987\n"
        b"ActiveState=inactive\n",
        b"ActiveState=active\nSubState=running\nMainPID=987\n"
        b"malformed-property\n",
        b"ActiveState=active\nSubState=running\nMainPID=987\n=empty-key\n",
        b"ActiveState=active\nSubState=running\nMainPID=987\ninvalid key=value\n",
        b"ActiveState=active\nSubState=running\nMainPID=987\ninvalid utf8=\xff\n",
    ],
)
async def test_duplicate_or_malformed_systemd_properties_fail_closed(payload: bytes) -> None:
    observation = await _observe_from(payload)
    assert observation.running is False
    assert observation.healthy is False
    assert observation.pid is None
    assert observation.started_at is None


@pytest.mark.asyncio
async def test_stop_settle_rejects_duplicate_or_malformed_systemd_properties() -> None:
    adapter = SystemdAdapter()

    async def run(_argv, **_kwargs):
        return 0, b"ActiveState=inactive\nActiveState=failed\nJob=\n", b""

    adapter._run = run
    assert await adapter._stop_job_settled(_profile()) is False


@pytest.mark.asyncio
async def test_observe_prefers_monotonic_activation_over_realtime(monkeypatch) -> None:
    fixed_now = 1_000_000.0
    monkeypatch.setattr(systemd_module.time, "monotonic", lambda: fixed_now)
    observation = await _observe_from(
        (
            f"ActiveState=active\nSubState=running\nMainPID=987\n"
            f"ExecMainStartTimestamp=Mon 2026-07-13 08:00:00 EDT\n"
            f"ExecMainStartTimestampMonotonic={int((fixed_now - 2) * 1_000_000)}\n"
        ).encode()
    )
    assert observation.started_at is not None
    assert observation.started_at.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_observe_falls_back_to_explicit_realtime_when_monotonic_unusable() -> None:
    observation = await _observe_from(
        b"ActiveState=active\nSubState=running\nMainPID=987\n"
        b"ExecMainStartTimestamp=2026-07-13T12:00:00+00:00\n"
        b"ExecMainStartTimestampMonotonic=not-a-number\n"
    )
    assert observation.started_at == datetime(2026, 7, 13, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["", "n/a", "0", "not-a-date", "Mon 2026-07-13 12:00:00"])
def test_parse_started_at_rejects_empty_invalid_and_naive_values(value) -> None:
    assert systemd_module._parse_started_at(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-13T12:00:00Z",
        "2026-07-13T12:00:00+00:00",
        "Mon 2026-07-13 12:00:00 UTC",
        "Mon 2026-07-13 12:00:00 GMT",
        "Mon 2026-07-13 08:00:00 -0400",
        "Mon 2026-07-13 17:30:00 +05:30",
    ],
)
def test_parse_started_at_accepts_aware_systemd_formats(value) -> None:
    parsed = systemd_module._parse_started_at(value)
    assert parsed is not None
    assert parsed.tzinfo is not None


@pytest.mark.parametrize(
    "value",
    [
        "Mon 2026-01-12 08:00:00 EST",
        "Mon 2026-07-13 08:00:00 EDT",
        "Mon 2026-07-13 08:00:00 CST",
    ],
)
def test_parse_started_at_rejects_unconfigured_abbreviations(value) -> None:
    assert systemd_module._parse_started_at(value) is None


@pytest.mark.parametrize("tz_name", ["UTC", "Pacific/Honolulu"])
def test_parse_started_at_is_independent_of_process_timezone(tz_name) -> None:
    script = (
        "from game_control.adapters.systemd import _parse_started_at\n"
        "print(_parse_started_at('Mon 2026-07-13 08:00:00 -0400').isoformat())\n"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    environment["TZ"] = tz_name
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "2026-07-13T08:00:00-04:00"


@pytest.mark.parametrize("value", ["", "not-a-pid", "0", "-1", "  "])
def test_parse_pid_returns_none_for_invalid_or_nonpositive_values(value) -> None:
    assert systemd_module._parse_pid(value) is None


def test_parse_pid_accepts_positive_pid() -> None:
    assert systemd_module._parse_pid(" 42 ") == 42


def test_parse_monotonic_start_time_rejects_bad_future_and_stale_values(monkeypatch) -> None:
    fixed_now = 1_000_000.0
    monkeypatch.setattr(systemd_module.time, "monotonic", lambda: fixed_now)
    assert systemd_module._parse_monotonic_started_at("not-a-number") is None
    assert systemd_module._parse_monotonic_started_at("0") is None
    assert systemd_module._parse_monotonic_started_at(str(int((fixed_now + 1) * 1_000_000))) is None
    assert (
        systemd_module._parse_monotonic_started_at(
            str(int((fixed_now - 365 * 24 * 3600 - 1) * 1_000_000))
        )
        is None
    )


def test_parse_monotonic_start_time_accepts_recent_value(monkeypatch) -> None:
    fixed_now = 1_000_000.0
    monkeypatch.setattr(systemd_module.time, "monotonic", lambda: fixed_now)
    parsed = systemd_module._parse_monotonic_started_at(str(int((fixed_now - 2) * 1_000_000)))
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_recent_logs_rejects_naive_time_ranges_before_running_journalctl() -> None:
    adapter = SystemdAdapter()

    async def run(*_args, **_kwargs):
        raise AssertionError("invalid time range reached journalctl")

    adapter._run = run
    with pytest.raises(AdapterError, match="timezone-aware"):
        await adapter.recent_logs(_profile(), 10, since=datetime(2026, 7, 13, 12, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("since", "until"),
    [
        (
            datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 13, 13, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 7, 13, 8, 0, tzinfo=timezone(timedelta(hours=-4))),
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone(timedelta(hours=-4))),
        ),
    ],
)
async def test_recent_logs_serializes_aware_cursors_with_explicit_utc(
    since: datetime, until: datetime
) -> None:
    adapter = SystemdAdapter()
    seen: list[tuple[str, ...]] = []

    async def run(argv, **_kwargs):
        seen.append(argv)
        return 0, b"", b""

    adapter._run = run
    await adapter.recent_logs(_profile(), 10, since=since, until=until)

    assert seen[0][-6:] == (
        "--since",
        "2026-07-13 12:00:00.000000 UTC",
        "--until",
        "2026-07-13 13:00:00.000000 UTC",
        "-n",
        "10",
    )


@pytest.mark.asyncio
async def test_recent_logs_maps_journal_failure_and_skips_hostile_json() -> None:
    adapter = SystemdAdapter()

    async def failed(_argv, **_kwargs):
        return 1, b"", b"journal unavailable"

    adapter._run = failed
    with pytest.raises(AdapterError, match="journal read failed"):
        await adapter.recent_logs(_profile(), 10)

    async def hostile(_argv, **_kwargs):
        return (
            0,
            b"not-json\n{}\n{\"__REALTIME_TIMESTAMP\":\"bad\"}\n"
            b'{"__REALTIME_TIMESTAMP":"1735689600000000","MESSAGE":"ok"}\n',
            b"",
        )

    adapter._run = hostile
    lines = await adapter.recent_logs(_profile(), 10)
    assert [(line.message, line.severity) for line in lines] == [("ok", "info")]


@pytest.mark.asyncio
async def test_stream_journal_handles_missing_streams() -> None:
    class Process:
        stdout = None
        stderr = None

        async def wait(self):
            pass

    stdout, stderr = await systemd_module._stream_journal(Process(), 10)
    assert stdout == b""
    assert stderr == b""


@pytest.mark.asyncio
async def test_bounded_communicate_supports_scalar_output_and_enforces_limit() -> None:
    class ScalarProcess:
        stdout = None
        stderr = None

        async def communicate(self):
            return b"out"

    assert await systemd_module._bounded_communicate(ScalarProcess()) == (b"out", b"")

    class HugeProcess(ScalarProcess):
        async def communicate(self):
            return b"x" * (systemd_module._MAX_OUTPUT + 1)

    with pytest.raises(AdapterError, match="output exceeded limit"):
        await systemd_module._bounded_communicate(HugeProcess())


@pytest.mark.asyncio
async def test_bounded_stream_io_enforces_limit_and_waits_after_success() -> None:
    class Stream:
        async def read(self, _limit):
            return b"ok"

    class Process:
        stdout = Stream()
        stderr = Stream()
        waited = False

        async def wait(self):
            self.waited = True

    process = Process()
    assert await systemd_module._bounded_communicate(process) == (b"ok", b"ok")
    assert process.waited is True

    class HugeStream:
        async def read(self, _limit):
            return b"x" * (systemd_module._MAX_OUTPUT + 1)

    class HugeProcess:
        stdout = HugeStream()
        stderr = Stream()
        killed = False
        waited = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True

    huge = HugeProcess()
    with pytest.raises(AdapterError, match="output exceeded limit"):
        await systemd_module._bounded_communicate(huge)
    assert huge.killed is True
    assert huge.waited is True


@pytest.mark.asyncio
async def test_write_input_requires_stdin() -> None:
    with pytest.raises(AdapterError, match="input unavailable"):
        await systemd_module._write_input(type("Process", (), {"stdin": None})(), b"list")
