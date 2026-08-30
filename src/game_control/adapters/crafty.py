from __future__ import annotations

import ast
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from ..models import AdapterKind, Profile
from ..protocol import LogLine
from .base import AdapterError, AdapterObservation

_MAX_LOG_BYTES = 256 * 1024
_MAX_VERSION_LENGTH = 128
_UNKNOWN_VERSION = "unknown"
_CA_PATH = Path("/etc/game-control/crafty-ca.pem")
_VERSION_KEYS = ("VERSION", "MINECRAFT_VERSION", "MC_VERSION", "SERVER_VERSION", "FORGE_VERSION")
_VERSION_TOKEN = re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?$")
_VERSION_IN_TEXT = re.compile(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9_.-]+)?")


class CraftyAdapter:
    """Fixed-route Crafty v2 adapter.

    The server UUID comes exclusively from the validated root Profile.  No
    action, URL, path, or command is accepted from an RPC request.
    """

    _ROUTES = {
        "stats": ("GET", "/api/v2/servers/{id}/stats"),
        "logs": ("GET", "/api/v2/servers/{id}/logs"),
        "start": ("POST", "/api/v2/servers/{id}/action/start_server"),
        "stop": ("POST", "/api/v2/servers/{id}/action/stop_server"),
        "restart": ("POST", "/api/v2/servers/{id}/action/restart_server"),
        "kill": ("POST", "/api/v2/servers/{id}/action/kill_server"),
    }

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify: str | bool | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        if not token or "\x00" in token or "\n" in token:
            raise ValueError("invalid Crafty credential")
        self.base_url = base_url.rstrip("/")
        self.token = token
        if verify is None:
            verify = str(_CA_PATH) if _CA_PATH.is_file() else True
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify,
            timeout=httpx.Timeout(10.0),
            headers={"Authorization": f"Bearer {token}"},
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def route(self, operation: str, profile: Profile) -> tuple[str, str]:
        self._validate_profile(profile)
        try:
            method, template = self._ROUTES[operation]
        except KeyError as exc:
            raise KeyError("unsupported Crafty operation") from exc
        return method, template.format(id=str(profile.crafty_server_id))

    async def _request(self, operation: str, profile: Profile) -> Any:
        method, path = self.route(operation, profile)
        try:
            chunks: list[bytes] = []
            total = 0
            if hasattr(self._client, "stream"):
                async with self._client.stream(method, path) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_LOG_BYTES:
                            raise AdapterError("Crafty response exceeded limit", retryable=False)
                        chunks.append(chunk)
            else:
                response = await self._client.request(method, path)
                response.raise_for_status()
                body = getattr(response, "content", b"") or b""
                if len(body) > _MAX_LOG_BYTES:
                    raise AdapterError("Crafty response exceeded limit", retryable=False)
                chunks.append(body)
        except (httpx.HTTPError, OSError) as exc:
            raise AdapterError("Crafty request failed") from exc
        except AdapterError:
            raise
        try:
            body = b"".join(chunks)
            return __import__("json").loads(body.decode("utf-8")) if body else {}
        except ValueError:
            return {}

    async def start(self, profile: Profile) -> None:
        await self._request("start", profile)

    async def graceful_stop(self, profile: Profile) -> None:
        await self._request("stop", profile)

    async def stop(self, profile: Profile) -> None:
        await self.graceful_stop(profile)

    async def force_stop(self, profile: Profile) -> None:
        await self._request("kill", profile)

    async def send_command(self, profile: Profile, command: str) -> None:
        self._validate_profile(profile)
        if (
            not command
            or len(command) > 512
            or command.strip(" ") != command
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in command)
        ):
            raise AdapterError("Crafty command unsupported", retryable=False)
        try:
            response = await self._client.request(
                "POST",
                f"/api/v2/servers/{profile.crafty_server_id}/stdin/",
                content=command.encode("utf-8"),
            )
            response.raise_for_status()
        except (UnicodeEncodeError, httpx.HTTPError, OSError) as exc:
            raise AdapterError("Crafty command failed", retryable=True) from exc

    async def observe(self, profile: Profile) -> AdapterObservation:
        payload = await self._request("stats", profile)
        if not isinstance(payload, dict):
            return AdapterObservation(running=False, healthy=None)
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
        running = _first_bool(payload, "running", "is_running", "online")
        if running is None:
            state = _first_string(payload, "state", "status")
            running = state.lower() in {"running", "online", "started"} if state else False
        pid = _first_int(payload, "pid", "process_id")
        players = _first_int(payload, "players_online", "online_players", "online", "players")
        crashed = _first_bool(payload, "crashed", "is_crashed")
        ping_ok = _ping_health(payload.get("int_ping_results"))
        healthy = _first_bool(payload, "healthy", "health")
        if healthy is None:
            if bool(running) and crashed is False and ping_ok is True:
                healthy = True
            elif crashed is True or ping_ok is False:
                healthy = False
            else:
                healthy = None
        return AdapterObservation(
            running=running,
            healthy=healthy,
            pid=pid,
            started_at=_first_datetime(payload, "started_at", "start_time", "started"),
            players_online=players,
            player_names=_extract_player_names(payload),
            installed_version=parse_version_text(_first_value(payload, "version", "installed_version")),
            required_ports_ready=_first_bool(payload, "required_ports_ready", "ports_ready"),
        )

    async def recent_logs(
        self,
        profile: Profile,
        limit: int,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[LogLine]:
        del since, until
        bounded = max(1, min(int(limit), 5000))
        payload = await self._request("logs", profile)
        if isinstance(payload, dict):
            payload = payload.get("logs", payload.get("data", []))
        if isinstance(payload, str):
            payload = payload.encode()[:_MAX_LOG_BYTES].decode("utf-8", "replace").splitlines()
        if not isinstance(payload, list):
            return []
        output: list[LogLine] = []
        now = datetime.now(timezone.utc)
        for item in payload[-bounded:]:
            if isinstance(item, str):
                output.append(LogLine(timestamp=now, severity="info", message=item[:8192]))
            elif isinstance(item, dict):
                message = item.get("message", item.get("line", ""))
                if not isinstance(message, str):
                    continue
                timestamp = item.get("timestamp", now)
                severity = item.get("severity", "info")
                try:
                    timestamp = _parse_log_timestamp(timestamp)
                    if timestamp is None:
                        continue
                    output.append(
                        LogLine(
                            timestamp=timestamp,
                            severity=severity if severity in {"debug", "info", "warning", "error"} else "info",
                            message=message[:8192],
                        )
                    )
                except (TypeError, ValueError):
                    continue
        return output

    @staticmethod
    def _validate_profile(profile: Profile) -> None:
        if profile.adapter is not AdapterKind.CRAFTY or profile.crafty_server_id is None:
            raise AdapterError("profile is not a Crafty profile", retryable=False)


def _first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _extract_player_names(payload: dict[str, Any]) -> tuple[str, ...] | None:
    for key in ("players", "online_players", "player_list"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            names = [str(item).strip() for item in value if str(item).strip()]
            return tuple(names)
        if isinstance(value, str):
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, (list, tuple)):
                return tuple(str(item).strip() for item in parsed if str(item).strip())
    return None


def _first_bool(payload: dict[str, Any], *keys: str) -> bool | None:
    value = _first_value(payload, *keys)
    return value if isinstance(value, bool) else None


def _first_int(payload: dict[str, Any], *keys: str) -> int | None:
    value = _first_value(payload, *keys)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    value = _first_value(payload, *keys)
    if not isinstance(value, str) or len(value) > 256:
        return None
    return None if value.casefold() in {"false", "none", "null", ""} else value


def _ping_health(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None
    if isinstance(value, dict):
        return all(bool(item) for item in value.values()) if value else None
    if isinstance(value, list):
        return all(bool(item) for item in value) if value else None
    return None


def parse_version_text(value: Any) -> str | None:
    """Extract a bounded server version from a scalar or variables.txt body."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    lines = text.splitlines()
    fields: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        fields[key.strip().upper()] = candidate.strip().strip('"').strip("'")

    for key in _VERSION_KEYS:
        candidate = fields.get(key)
        if candidate and _VERSION_TOKEN.fullmatch(candidate):
            return candidate[:_MAX_VERSION_LENGTH]

    for key in ("SERVER_JAR", "JAR_FILE", "JAR"):
        candidate = fields.get(key)
        if candidate:
            match = _VERSION_IN_TEXT.search(candidate)
            if match:
                return match.group(0)[:_MAX_VERSION_LENGTH]

    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#") or "=" in candidate:
            continue
        match = _VERSION_IN_TEXT.search(candidate)
        if match:
            return match.group(0)[:_MAX_VERSION_LENGTH]

    if len(lines) == 1:
        if _VERSION_TOKEN.fullmatch(text):
            return text[:_MAX_VERSION_LENGTH]
        match = _VERSION_IN_TEXT.search(text)
        if match:
            return match.group(0)[:_MAX_VERSION_LENGTH]
    return _UNKNOWN_VERSION


def _first_datetime(payload: dict[str, Any], *keys: str) -> datetime | None:
    value = _first_value(payload, *keys)
    return _parse_log_timestamp(value)


def _parse_log_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
