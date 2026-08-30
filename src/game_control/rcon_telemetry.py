"""Privacy-safe persistent RCON transport for bounded Minecraft telemetry."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from .rcon import (
    RCON_HOST,
    RCON_MAX_PACKET,
    RCON_PASSWORD_PATH,
    RCON_PORT,
    RCON_SERVERDATA_AUTH,
    RCON_SERVERDATA_AUTH_RESPONSE,
    RCON_SERVERDATA_EXECCOMMAND,
    RCON_SERVERDATA_RESPONSE_VALUE,
    RconError,
    _packet,
    _read_packet,
    _read_password,
)

RCON_TELEMETRY_TIMEOUT_SECONDS = 5.0
RCON_TELEMETRY_MAX_RESPONSE_BYTES = 8192
RCON_TELEMETRY_MAX_ATTEMPTS = 3
RCON_TELEMETRY_BACKOFF_INITIAL = 0.1
RCON_TELEMETRY_BACKOFF_MAX = 2.0

_PLAYER_COUNT = re.compile(r"^There are (\d{1,6}) of a max of (\d{1,6}) players online(?::.*)?$")
_TPS = re.compile(r"(?:Mean TPS|TPS)\s*:\s*(\d{1,3}(?:\.\d{1,3})?)", re.IGNORECASE)
_MSPT = re.compile(r"(?:Mean tick time|MSPT)\s*:\s*(\d{1,6}(?:\.\d{1,3})?)", re.IGNORECASE)


class TelemetryCommand(str, Enum):
    PLAYER_COUNT = "player_count"
    PERFORMANCE = "performance"
    TPS = "tps"
    MSPT = "mspt"
    VERSION = "version"


class TelemetryErrorCode(str, Enum):
    NONE = "none"
    INACTIVE = "inactive"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"
    RESPONSE_LIMIT = "response_limit"
    BACKOFF = "backoff"


@dataclass(frozen=True)
class PlayerCountResult:
    online: int
    maximum: int


@dataclass(frozen=True)
class PerformanceResult:
    tps: float | None = None
    mspt: float | None = None


@dataclass(frozen=True)
class UnavailableResult:
    available: bool = False


TelemetryResult = PlayerCountResult | PerformanceResult | UnavailableResult
RconTelemetryCommand = TelemetryCommand


@dataclass(frozen=True)
class RconTelemetryHealth:
    requests: int
    successes: int
    failures: int
    reconnects: int
    timeouts: int
    response_limit_failures: int
    consecutive_failures: int
    last_success_age_seconds: float | None
    last_error: TelemetryErrorCode
    connected: bool
    active: bool


Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float], float]


class PersistentRconTelemetry:
    """One serialized, loopback-only channel for one running profile identity."""

    def __init__(
        self,
        profile_id: str,
        *,
        host: str = RCON_HOST,
        port: int = RCON_PORT,
        password_path: Any = RCON_PASSWORD_PATH,
        timeout: float = RCON_TELEMETRY_TIMEOUT_SECONDS,
        max_response_bytes: int = RCON_TELEMETRY_MAX_RESPONSE_BYTES,
        max_attempts: int = RCON_TELEMETRY_MAX_ATTEMPTS,
        open_connection: Callable[..., Any] | None = None,
        password_reader: Callable[[Any], str] = _read_password,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = lambda value: value,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 128:
            raise ValueError("profile identity is invalid")
        if host != RCON_HOST or port != RCON_PORT:
            raise ValueError("RCON telemetry endpoint is not approved")
        if password_path != RCON_PASSWORD_PATH:
            raise ValueError("RCON telemetry credential path is not approved")
        if not math.isfinite(float(timeout)):
            raise ValueError("RCON telemetry timeout is invalid")
        if max_response_bytes < 1 or max_response_bytes > RCON_MAX_PACKET:
            raise ValueError("RCON telemetry response limit is invalid")
        if max_attempts < 1 or max_attempts > RCON_TELEMETRY_MAX_ATTEMPTS:
            raise ValueError("RCON telemetry attempt limit is invalid")
        self.profile_id = profile_id
        self.host, self.port, self.password_path = host, port, password_path
        self.timeout = max(0.1, min(float(timeout), RCON_TELEMETRY_TIMEOUT_SECONDS))
        self.max_response_bytes, self.max_attempts = max_response_bytes, max_attempts
        self._open_connection = open_connection or asyncio.open_connection
        self._password_reader, self._sleep, self._jitter, self._clock = password_reader, sleep, jitter, clock
        self._reader: asyncio.StreamReader | None = None
        self._writer: Any = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._active = True
        self._requests = self._successes = self._failures = 0
        self._reconnects = self._timeouts = self._response_limit_failures = 0
        self._consecutive_failures = 0
        self._last_success_at: float | None = None
        self._last_error = TelemetryErrorCode.NONE

    @property
    def health(self) -> RconTelemetryHealth:
        age = None if self._last_success_at is None else max(0.0, self._clock() - self._last_success_at)
        return RconTelemetryHealth(
            self._requests, self._successes, self._failures, self._reconnects,
            self._timeouts, self._response_limit_failures, self._consecutive_failures,
            age, self._last_error, self._reader is not None and self._writer is not None,
            self._active,
        )

    async def set_active(self, active: bool) -> None:
        async with self._lock:
            self._active = bool(active)
            if not self._active:
                await self._disconnect_locked()

    async def set_profile_identity(self, profile_id: str) -> None:
        if not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 128:
            raise ValueError("profile identity is invalid")
        async with self._lock:
            if profile_id != self.profile_id:
                self.profile_id = profile_id
                await self._disconnect_locked()

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def execute(self, command: TelemetryCommand) -> TelemetryResult:
        if not isinstance(command, TelemetryCommand):
            raise RconError("RCON telemetry command is not approved")
        if command is TelemetryCommand.VERSION:
            return UnavailableResult()
        async with self._lock:
            self._requests += 1
            if not self._active:
                self._record_failure(TelemetryErrorCode.INACTIVE)
                raise RconError("RCON telemetry channel is inactive")
            last: RconError | None = None
            for attempt in range(self.max_attempts):
                try:
                    if self._reader is None or self._writer is None:
                        await self._connect_locked()
                    raw = await self._request_locked(_wire_command(command))
                    result = _sanitize_result(command, raw)
                    self._successes += 1
                    self._consecutive_failures = 0
                    self._last_error = TelemetryErrorCode.NONE
                    self._last_success_at = self._clock()
                    return result
                except RconError as exc:
                    last = exc
                    code = _error_code(exc)
                    if code is TelemetryErrorCode.TIMEOUT:
                        self._timeouts += 1
                    if code is TelemetryErrorCode.RESPONSE_LIMIT:
                        self._response_limit_failures += 1
                    await self._disconnect_locked()
                    if attempt + 1 < self.max_attempts:
                        self._reconnects += 1
                        delay = RCON_TELEMETRY_BACKOFF_INITIAL * (2**attempt)
                        value = float(self._jitter(delay))
                        if not math.isfinite(value):
                            last = RconError("RCON telemetry backoff was invalid")
                            break
                        await self._sleep(min(RCON_TELEMETRY_BACKOFF_MAX, max(0.0, value)))
            self._record_failure(_error_code(last))
            raise last or RconError("RCON telemetry request failed")

    query = execute

    async def _connect_locked(self) -> None:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                self._open_connection(self.host, self.port, limit=RCON_MAX_PACKET + 4), self.timeout
            )
            password = self._password_reader(self.password_path)
            auth_id = self._next_id()
            writer.write(_packet(auth_id, RCON_SERVERDATA_AUTH, password))
            await asyncio.wait_for(writer.drain(), self.timeout)
            response_id, response_type, _ = await asyncio.wait_for(_read_packet(reader), self.timeout)
            if response_type != RCON_SERVERDATA_AUTH_RESPONSE or response_id != auth_id:
                raise RconError("RCON telemetry authentication failed")
            self._reader, self._writer = reader, writer
            writer = None
        except RconError:
            raise
        except asyncio.TimeoutError as exc:
            raise RconError("RCON telemetry request timed out") from exc
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            raise RconError("RCON telemetry connection failed") from exc
        finally:
            if writer is not None:
                await _close_writer(writer)

    async def _request_locked(self, command: str) -> str:
        assert self._reader is not None and self._writer is not None
        request_id = self._next_id()
        self._writer.write(_packet(request_id, RCON_SERVERDATA_EXECCOMMAND, command))
        try:
            await asyncio.wait_for(self._writer.drain(), self.timeout)
            response_id, response_type, response = await asyncio.wait_for(_read_packet(self._reader), self.timeout)
        except asyncio.TimeoutError as exc:
            raise RconError("RCON telemetry request timed out") from exc
        except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
            raise RconError("RCON telemetry request failed") from exc
        if response_id != request_id or response_type not in (RCON_SERVERDATA_RESPONSE_VALUE, RCON_SERVERDATA_AUTH_RESPONSE):
            raise RconError("RCON telemetry response was invalid")
        if len(response.encode("utf-8")) > self.max_response_bytes:
            raise RconError("RCON telemetry response exceeded limit")
        return response

    async def _disconnect_locked(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            await _close_writer(writer)

    def _next_id(self) -> int:
        self._request_id = self._request_id % 2_000_000_000 + 1
        return self._request_id

    def _record_failure(self, code: TelemetryErrorCode) -> None:
        self._failures += 1
        self._consecutive_failures += 1
        self._last_error = code


async def _close_writer(writer: Any) -> None:
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        try:
            await wait_closed()
        except (OSError, RuntimeError):
            pass


def _wire_command(command: TelemetryCommand) -> str:
    return "list" if command is TelemetryCommand.PLAYER_COUNT else "tps"


def _sanitize_result(command: TelemetryCommand, raw: str) -> TelemetryResult:
    if command is TelemetryCommand.PLAYER_COUNT:
        match = _PLAYER_COUNT.fullmatch(raw.strip())
        if match is None:
            raise RconError("RCON telemetry response was invalid")
        online, maximum = map(int, match.groups())
        if online > maximum or maximum > 1_000_000:
            raise RconError("RCON telemetry response was invalid")
        return PlayerCountResult(online, maximum)
    if command is TelemetryCommand.PERFORMANCE:
        tps_match, mspt_match = _TPS.search(raw), _MSPT.search(raw)
        if tps_match is None or mspt_match is None:
            raise RconError("RCON telemetry response was invalid")
        tps, mspt = float(tps_match.group(1)), float(mspt_match.group(1))
        if not 0 <= tps <= 1000 or not 0 <= mspt <= 1_000_000:
            raise RconError("RCON telemetry response was invalid")
        return PerformanceResult(tps=tps, mspt=mspt)
    pattern = _TPS if command is TelemetryCommand.TPS else _MSPT
    match = pattern.search(raw)
    if match is None:
        raise RconError("RCON telemetry response was invalid")
    value = float(match.group(1))
    if not math.isfinite(value) or value < 0 or (command is TelemetryCommand.TPS and value > 1000) or value > 1_000_000:
        raise RconError("RCON telemetry response was invalid")
    return PerformanceResult(tps=value if command is TelemetryCommand.TPS else None, mspt=value if command is TelemetryCommand.MSPT else None)


def _error_code(error: Exception | None) -> TelemetryErrorCode:
    message = "" if error is None else str(error)
    if "inactive" in message:
        return TelemetryErrorCode.INACTIVE
    if "authentication" in message:
        return TelemetryErrorCode.AUTHENTICATION
    if "timed out" in message:
        return TelemetryErrorCode.TIMEOUT
    if "exceeded limit" in message:
        return TelemetryErrorCode.RESPONSE_LIMIT
    if "backoff" in message:
        return TelemetryErrorCode.BACKOFF
    if "response" in message:
        return TelemetryErrorCode.INVALID_RESPONSE
    return TelemetryErrorCode.CONNECTION


RconTelemetryChannel = PersistentRconTelemetry

__all__ = [
    "TelemetryCommand", "RconTelemetryCommand", "TelemetryErrorCode", "PlayerCountResult",
    "PerformanceResult", "UnavailableResult", "RconTelemetryHealth", "PersistentRconTelemetry",
    "RconTelemetryChannel", "RCON_TELEMETRY_MAX_RESPONSE_BYTES",
]
