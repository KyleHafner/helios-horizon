from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import AdapterKind, Profile
from ..protocol import LogLine
from .base import AdapterError, AdapterObservation

_MAX_OUTPUT = 256 * 1024
_MAX_INPUT = 4096
_MAX_JOURNAL_RECORD = 64 * 1024
_STREAM_READER_LIMIT = 1024 * 1024
_COMMAND_TIMEOUT = 10.0
_CONSOLE_COMMAND_HELPER = "/usr/local/libexec/game-console-command"
_CONSOLE_PROFILES = frozenset(
    {"pz-rising", "terraria-vanilla", "terraria-tmod", "terraria-tmod-145-candidate"}
)
_RCON_PROFILE = "minecraft-sunlit-cobblemon"
_RCON_LIST_RESPONSE = re.compile(
    r"\AThere are (?P<count>\d{1,5}) of a max of (?P<maximum>\d{1,5}) players online:?"
    r"(?: (?P<names>.*))?\Z"
)
_MINECRAFT_PLAYER_NAME = re.compile(r"\A[A-Za-z0-9_]{1,16}\Z")
_SYSTEMD_ZONE_OFFSETS = {
    "UTC": 0,
    "GMT": 0,
    # Helios runs in America/New_York. Keep this fallback deliberately narrow:
    # abbreviations such as CST and BST are ambiguous across regions.
    "EST": -5,
    "EDT": -4,
}


def _systemd_fields(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip()
    return fields


class SystemdAdapter:
    """Fixed argv-only systemd/journalctl adapter."""

    def __init__(self, *, rcon: Any | None = None):
        self.rcon = rcon

    def command(self, operation: str, profile: Profile) -> tuple[str, ...]:
        unit = self._unit(profile)
        if operation == "start":
            return ("/usr/bin/systemctl", "start", unit)
        if operation == "stop":
            return ("/usr/bin/systemctl", "stop", unit)
        if operation in {"kill", "force_stop"}:
            return ("/usr/bin/systemctl", "kill", "-s", "SIGKILL", unit)
        if operation == "reset_failed":
            return ("/usr/bin/systemctl", "reset-failed", unit)
        if operation == "settle_state":
            return (
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=ActiveState,SubState,Job",
            )
        if operation == "observe":
            return (
                "/usr/bin/systemctl",
                "show",
                unit,
                "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,ExecMainStartTimestampMonotonic",
            )
        if operation == "logs":
            return (
                "/usr/bin/journalctl",
                "--namespace=horizon",
                "-u",
                unit,
                "--no-pager",
                "-o",
                "json",
            )
        raise KeyError("unsupported systemd operation")

    async def _run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float = _COMMAND_TIMEOUT,
        input_data: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        if input_data is not None and (
            not isinstance(input_data, bytes) or len(input_data) > _MAX_INPUT
        ):
            raise AdapterError("systemd input exceeded limit", retryable=False)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                limit=_STREAM_READER_LIMIT,
            )
            if argv and argv[0] == "/usr/bin/journalctl":
                if input_data is not None:
                    await _write_input(process, input_data)
                stdout, stderr = await asyncio.wait_for(
                    _stream_journal(process, _journal_limit(argv)), timeout=timeout
                )
            else:
                stdout, stderr = await asyncio.wait_for(
                    _bounded_communicate(process, input_data=input_data), timeout=timeout
                )
        except asyncio.TimeoutError as exc:
            await _kill_and_wait(process)
            raise AdapterError("systemd command timed out") from exc
        except asyncio.CancelledError:
            await _kill_and_wait(process)
            raise
        except AdapterError:
            await _kill_and_wait(process)
            raise
        except (OSError, ValueError) as exc:
            await _kill_and_wait(process)
            raise AdapterError("systemd command failed") from exc
        return getattr(process, "returncode", 0) or 0, stdout, stderr

    async def start(self, profile: Profile) -> None:
        code, _, _ = await self._run(
            self.command("start", profile),
            timeout=profile.start_timeout_seconds + 5,
        )
        if code:
            raise AdapterError("systemd start failed", retryable=False, returncode=code)

    async def graceful_stop(self, profile: Profile) -> None:
        code, _, _ = await self._run(
            self.command("stop", profile),
            timeout=profile.stop_timeout_seconds + 5,
        )
        if code:
            raise AdapterError("systemd stop failed", retryable=False, returncode=code)

    async def stop(self, profile: Profile) -> None:
        await self.graceful_stop(profile)

    async def force_stop(self, profile: Profile) -> None:
        code, _, _ = await self._run(
            self.command("kill", profile),
            timeout=profile.stop_timeout_seconds + 5,
        )
        if code:
            raise AdapterError("systemd force stop failed", retryable=False, returncode=code)
        # ``kill`` returns before ExecStopPost has necessarily completed.  Do
        # not use AdapterObservation.running here: it is already false while a
        # unit is deactivating, which allowed reset-failed to race the final
        # stop result and leave the unit failed afterward.
        deadline = time.monotonic() + min(float(profile.stop_timeout_seconds), 15.0)
        while time.monotonic() < deadline:
            if await self._stop_job_settled(profile):
                break
            await asyncio.sleep(0.1)
        else:
            raise AdapterError("systemd force stop did not settle", retryable=False)
        code, _, _ = await self._run(self.command("reset_failed", profile))
        if code:
            raise AdapterError("systemd force stop cleanup failed", retryable=False, returncode=code)
        if not await self._stop_job_settled(profile, require_inactive=True):
            raise AdapterError("systemd force stop cleanup did not settle", retryable=False)

    async def _stop_job_settled(self, profile: Profile, *, require_inactive: bool = False) -> bool:
        code, stdout, _ = await self._run(self.command("settle_state", profile))
        if code:
            return False
        fields = _systemd_fields(stdout)
        active = fields.get("ActiveState", "")
        job = fields.get("Job", "")
        allowed = {"inactive"} if require_inactive else {"inactive", "failed"}
        return active in allowed and not job

    async def send_command(self, profile: Profile, command: str) -> None:
        profile_id = str(profile.id)
        if profile_id == _RCON_PROFILE:
            if self.rcon is None:
                raise AdapterError("systemd command unsupported", retryable=False)
            try:
                await self.rcon.execute(command)
            except Exception as exc:
                # The transport owns credential and endpoint redaction. Keep
                # adapter failures equally opaque to the controller/audit/UI.
                raise AdapterError("RCON command failed", retryable=True) from exc
            return
        if (
            profile.adapter is not AdapterKind.SYSTEMD
            or profile_id not in _CONSOLE_PROFILES
            or not command
            or len(command) > 512
            or command.strip(" ") != command
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in command)
        ):
            raise AdapterError("systemd command unsupported", retryable=False)
        try:
            input_data = command.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AdapterError("systemd command unsupported", retryable=False) from exc
        code, _, _ = await self._run(
            (_CONSOLE_COMMAND_HELPER, profile_id),
            timeout=_COMMAND_TIMEOUT,
            input_data=input_data,
        )
        if code:
            raise AdapterError("systemd command failed", retryable=True, returncode=code)

    async def observe(self, profile: Profile) -> AdapterObservation:
        code, stdout, _ = await self._run(self.command("observe", profile))
        if code:
            return AdapterObservation(running=False, healthy=False)
        fields = _systemd_fields(stdout)
        active = fields.get("ActiveState", "")
        substate = fields.get("SubState", "")
        pid = _parse_pid(fields.get("MainPID", ""))
        running = active == "active" and substate in {"running", "listening", "exited"}
        started_at = _parse_started_at(fields.get("ExecMainStartTimestamp", ""))
        if started_at is None:
            started_at = _parse_monotonic_started_at(fields.get("ExecMainStartTimestampMonotonic", ""))
        players_online = None
        player_names = None
        profile_id = getattr(profile.id, "value", str(profile.id))
        if running and profile_id == _RCON_PROFILE and self.rcon is not None:
            try:
                parsed_players = _parse_rcon_player_list(await self.rcon.execute("list"))
                if parsed_players is not None:
                    players_online, player_names = parsed_players
            except Exception:
                # Unknown must remain fail-closed for stop/switch fencing.
                players_online = None
                player_names = None
        return AdapterObservation(
            running=running,
            healthy=running,
            pid=pid,
            started_at=started_at,
            players_online=players_online,
            player_names=player_names,
        )

    async def recent_logs(
        self,
        profile: Profile,
        limit: int,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[LogLine]:
        bounded = max(1, min(int(limit), 5000))
        argv = self.command("logs", profile)
        for flag, value in (("--since", since), ("--until", until)):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise AdapterError("log range must be timezone-aware", retryable=False)
                # journalctl treats a timezone-less timestamp as local time.
                # Keep the normalized UTC value explicit so hosts in a DST
                # timezone do not shift incremental log windows.
                argv += (
                    flag,
                    value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f UTC"),
                )
        code, stdout, _ = await self._run(argv + ("-n", str(bounded)))
        if code:
            raise AdapterError("journal read failed")
        lines = stdout.decode("utf-8", "replace").splitlines()
        output: list[LogLine] = []
        for line in lines[-bounded:]:
            try:
                payload = json.loads(line)
                micros = int(payload["__REALTIME_TIMESTAMP"])
                timestamp = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)
                message = str(payload.get("MESSAGE", ""))[:8192]
                priority = int(payload.get("PRIORITY", 6))
                severity = (
                    "error" if 0 <= priority <= 3
                    else "warning" if priority == 4
                    else "info" if 5 <= priority <= 6
                    else "debug" if priority == 7
                    else "info"
                )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            output.append(LogLine(timestamp=timestamp, severity=severity, message=message))
        return output

    @staticmethod
    def _unit(profile: Profile) -> str:
        if profile.adapter is not AdapterKind.SYSTEMD or not profile.systemd_unit:
            raise AdapterError("profile is not a systemd profile", retryable=False)
        return profile.systemd_unit


def _parse_pid(value: str) -> int | None:
    try:
        pid = int(value.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def _parse_rcon_player_count(value: Any) -> int | None:
    parsed = _parse_rcon_player_list(value)
    return parsed[0] if parsed is not None else None


def _parse_rcon_player_list(value: Any) -> tuple[int, tuple[str, ...] | None] | None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    match = _RCON_LIST_RESPONSE.fullmatch(value.strip())
    if match is None:
        return None
    count = int(match.group("count"))
    maximum = int(match.group("maximum"))
    if maximum > 10_000 or count > maximum:
        return None
    raw_names = (match.group("names") or "").strip()
    if not raw_names:
        return count, () if count == 0 else None
    names = tuple(name.strip() for name in raw_names.split(","))
    if (
        len(names) != count
        or len(set(names)) != len(names)
        or any(_MINECRAFT_PLAYER_NAME.fullmatch(name) is None for name in names)
    ):
        return count, None
    return count, names


def _parse_started_at(value: str) -> datetime | None:
    value = value.strip()
    if not value or value in {"n/a", "0"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Python's %Z parser only accepts UTC/GMT and the process-local names;
        # on a UTC CI runner it rejects a valid systemd `... EDT` value.  The
        # deployment-local abbreviations emitted by systemd have fixed offsets
        # here, so parse them without consulting the runner's TZ.
        zone = value.rsplit(" ", 1)[-1]
        offset_hours = _SYSTEMD_ZONE_OFFSETS.get(zone)
        if offset_hours is not None:
            try:
                parsed = datetime.strptime(
                    value.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S"
                ).replace(
                    tzinfo=timezone(timedelta(hours=offset_hours), name=zone)
                )
                return parsed
            except ValueError:
                return None
        for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S %z"):
            try:
                parsed = datetime.strptime(value, fmt)
                if fmt.endswith("%Z"):
                    parsed = parsed.astimezone()
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo is not None else None


def _parse_monotonic_started_at(value: str) -> datetime | None:
    try:
        micros = int(value.strip())
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    age = time.monotonic() - micros / 1_000_000
    if age < 0 or age > 365 * 24 * 3600:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=age)


def _journal_limit(argv: tuple[str, ...]) -> int:
    try:
        return max(1, min(int(argv[argv.index("-n") + 1]), 5000))
    except (ValueError, IndexError, TypeError):
        return 5000


async def _stream_journal(process: Any, limit: int) -> tuple[bytes, bytes]:
    records: deque[bytes] = deque(maxlen=limit)

    async def read_stdout() -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                # asyncio.StreamReader.readline translates its internal
                # LimitOverrunError to ValueError after discarding the record.
                continue
            if not line:
                break
            if len(line) <= _MAX_JOURNAL_RECORD and line.endswith(b"\n"):
                records.append(line.rstrip(b"\n"))

    async def read_stderr() -> bytes:
        stream = getattr(process, "stderr", None)
        if stream is None:
            return b""
        return (await stream.read(_MAX_OUTPUT + 1))[:_MAX_OUTPUT]

    _, stderr = await asyncio.gather(read_stdout(), read_stderr())
    await process.wait()
    return b"\n".join(records), stderr


async def _kill_and_wait(process: Any) -> None:
    if process is None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError, ValueError):
        pass
    try:
        await process.wait()
    except (ProcessLookupError, OSError, ValueError):
        pass


async def _bounded_communicate(
    process: Any, *, input_data: bytes | None = None
) -> tuple[bytes, bytes]:
    """Read bounded output and terminate a process that floods its pipes."""

    if not hasattr(getattr(process, "stdout", None), "read") and hasattr(process, "communicate"):
        if input_data is None:
            output = await process.communicate()
        else:
            output = await process.communicate(input=input_data)
        if isinstance(output, tuple):
            stdout, stderr = output
        else:
            stdout, stderr = output, b""
        stdout = stdout or b""
        stderr = stderr or b""
        if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT:
            raise AdapterError("systemd output exceeded limit", retryable=False)
        return stdout, stderr

    if input_data is not None:
        await _write_input(process, input_data)

    async def read(stream):
        if stream is None:
            return b""
        return await stream.read(_MAX_OUTPUT + 1)

    stdout, stderr = await asyncio.gather(read(process.stdout), read(process.stderr))
    if len(stdout) > _MAX_OUTPUT or len(stderr) > _MAX_OUTPUT:
        try:
            process.kill()
            await process.wait()
        except (ProcessLookupError, OSError):
            pass
        raise AdapterError("systemd output exceeded limit", retryable=False)
    await process.wait()
    return stdout, stderr


async def _write_input(process: Any, input_data: bytes) -> None:
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        raise AdapterError("systemd input unavailable", retryable=False)
    stdin.write(input_data)
    drain = getattr(stdin, "drain", None)
    if drain is not None:
        await drain()
    stdin.close()
