"""Fixed loopback Minecraft RCON transport.

The controller owns the endpoint and credential location.  Callers provide
only a validated command; the password is read for the duration of one
bounded request and is never included in an exception, log, or return value.
"""

from __future__ import annotations

import asyncio
import os
import stat
import struct
from pathlib import Path
from typing import Any, Callable


RCON_HOST = "127.0.0.1"
RCON_PORT = 25575
RCON_PASSWORD_PATH = Path("/etc/game-control/secrets.d/minecraft-rcon-password")
RCON_TIMEOUT_SECONDS = 10.0
RCON_MAX_PACKET = 64 * 1024
RCON_MAX_PASSWORD_BYTES = 1024
RCON_SERVERDATA_AUTH = 3
RCON_SERVERDATA_EXECCOMMAND = 2
RCON_SERVERDATA_RESPONSE_VALUE = 0
RCON_SERVERDATA_AUTH_RESPONSE = 2


class RconError(RuntimeError):
    """A safe, redacted RCON transport error."""


def _read_password(path: Path = RCON_PASSWORD_PATH) -> str:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RconError("RCON credential is unavailable")
        with path.open("rb") as stream:
            raw = stream.read(RCON_MAX_PASSWORD_BYTES + 1)
        if len(raw) > RCON_MAX_PASSWORD_BYTES:
            raise RconError("RCON credential is unavailable")
        value = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RconError("RCON credential is unavailable") from exc
    value = value.rstrip("\n")
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise RconError("RCON credential is unavailable")
    return value


def _packet(request_id: int, packet_type: int, payload: str) -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    raw_length = await reader.readexactly(4)
    length = struct.unpack("<i", raw_length)[0]
    if length < 10 or length > RCON_MAX_PACKET:
        raise RconError("RCON response was invalid")
    body = await reader.readexactly(length)
    if body[-2:] != b"\x00\x00":
        raise RconError("RCON response was invalid")
    try:
        request_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8")
    except (struct.error, UnicodeDecodeError) as exc:
        raise RconError("RCON response was invalid") from exc
    return request_id, packet_type, payload


class RconClient:
    """One-shot, fixed loopback RCON client used by the root controller."""

    def __init__(
        self,
        *,
        host: str = RCON_HOST,
        port: int = RCON_PORT,
        password_path: Path = RCON_PASSWORD_PATH,
        timeout: float = RCON_TIMEOUT_SECONDS,
        open_connection: Callable[..., Any] | None = None,
        password_reader: Callable[[Path], str] = _read_password,
    ) -> None:
        if host != RCON_HOST or port != RCON_PORT:
            raise ValueError("RCON endpoint is not approved")
        if Path(password_path) != RCON_PASSWORD_PATH:
            raise ValueError("RCON credential path is not approved")
        self.host = host
        self.port = port
        self.password_path = Path(password_path)
        self.timeout = max(1.0, min(float(timeout), RCON_TIMEOUT_SECONDS))
        self._open_connection = open_connection or asyncio.open_connection
        self._password_reader = password_reader
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id = (self._request_id % 2_000_000_000) + 1
        return self._request_id

    async def execute(self, command: str, *, require_ack: bool = False) -> str:
        if (
            not isinstance(command, str)
            or not command
            or len(command) > 512
            or command.strip(" ") != command
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in command)
        ):
            raise RconError("RCON command is invalid")
        password = self._password_reader(self.password_path)
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                self._open_connection(self.host, self.port, limit=RCON_MAX_PACKET + 4),
                timeout=self.timeout,
            )
            auth_id = self._next_id()
            writer.write(_packet(auth_id, RCON_SERVERDATA_AUTH, password))
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            auth_deadline = asyncio.get_running_loop().time() + self.timeout
            response_id, response_type, response = await _read_packet_until(reader, auth_deadline)
            if response_type == RCON_SERVERDATA_RESPONSE_VALUE and not response:
                response_id, response_type, _ = await _read_packet_until(reader, auth_deadline)
            if response_type != RCON_SERVERDATA_AUTH_RESPONSE or response_id == -1 or response_id != auth_id:
                raise RconError("RCON authentication failed")
            command_id = self._next_id()
            writer.write(_packet(command_id, RCON_SERVERDATA_EXECCOMMAND, command))
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            response_id, response_type, response = await asyncio.wait_for(_read_packet(reader), timeout=self.timeout)
            if response_id != command_id or response_type not in {RCON_SERVERDATA_RESPONSE_VALUE, RCON_SERVERDATA_AUTH_RESPONSE}:
                raise RconError("RCON command acknowledgement was invalid")
            if require_ack and not response.strip():
                raise RconError("RCON command acknowledgement was missing")
            return response[:8192]
        except RconError:
            raise
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError) as exc:
            raise RconError("RCON request failed") from exc
        finally:
            if writer is not None:
                writer.close()
                wait_closed = getattr(writer, "wait_closed", None)
                if wait_closed is not None:
                    try:
                        await wait_closed()
                    except (OSError, RuntimeError):
                        pass


async def _read_packet_until(reader: asyncio.StreamReader, deadline: float) -> tuple[int, int, str]:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(_read_packet(reader), timeout=remaining)


class SunlitRconTransport:
    """Typed save controls for the online Sunlit backup path."""

    def __init__(self, client: RconClient):
        self.client = client

    async def save_off(self) -> None:
        await self.client.execute("save-off")

    async def save_all_flush(self) -> None:
        await self.client.execute("save-all flush", require_ack=True)

    async def save_on(self) -> None:
        await self.client.execute("save-on")


__all__ = [
    "RCON_HOST",
    "RCON_PORT",
    "RCON_PASSWORD_PATH",
    "RconClient",
    "RconError",
    "SunlitRconTransport",
]
