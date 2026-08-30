"""Protocol-aware game and public-relay health checks."""

from __future__ import annotations

import asyncio
import inspect
import math
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from enum import StrEnum

import psutil

from .adapters.base import AdapterError
from .introspection import signature_parameters
from .models import AdapterKind, HealthState, ProfileId

_MAX_READY_BYTES = 256 * 1024
_OBSERVATION_UNSET = object()
_PASSIVE_PUBLIC_PROBE_PROFILES = frozenset(
    {
        ProfileId.TERRARIA_VANILLA,
        ProfileId.TERRARIA_TMOD,
    }
)


class ReadinessOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class ReadinessTicket:
    profile_id: str
    generation: int


@dataclass(slots=True)
class _ReadinessEntry:
    event: asyncio.Event
    outcome: ReadinessOutcome | None = None
    active: bool = True


class ReadinessCoordinator:
    """One bounded asyncio notification slot per internally started profile."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[int, _ReadinessEntry]] = {}
        self._generation = 0
        self._counts = {outcome: 0 for outcome in ReadinessOutcome}

    @staticmethod
    def _profile_key(profile_id: Any) -> str:
        value = getattr(profile_id, "value", profile_id)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError("invalid readiness profile")
        return value

    def begin(self, profile_id: Any) -> ReadinessTicket:
        key = self._profile_key(profile_id)
        self._generation += 1
        ticket = ReadinessTicket(key, self._generation)
        self._entries[key] = (ticket.generation, _ReadinessEntry(asyncio.Event()))
        return ticket

    def notify(self, ticket: ReadinessTicket, outcome: ReadinessOutcome) -> bool:
        entry = self._entries.get(ticket.profile_id)
        if entry is None or entry[0] != ticket.generation or entry[1].outcome is not None:
            return False
        entry[1].outcome = ReadinessOutcome(outcome)
        self._counts[entry[1].outcome] += 1
        entry[1].event.set()
        return True

    def latest(self, profile_id: Any, *, generation: int | None = None) -> ReadinessTicket:
        key = self._profile_key(profile_id)
        entry = self._entries.get(key)
        if entry is None:
            raise RuntimeError("readiness notification is unavailable")
        if generation is not None and (isinstance(generation, bool) or generation != entry[0]):
            raise RuntimeError("readiness generation is stale")
        return ReadinessTicket(key, entry[0])

    async def wait(self, ticket: ReadinessTicket, *, timeout: float) -> ReadinessOutcome:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 900:
            raise ValueError("invalid readiness timeout")
        entry = self._entries.get(ticket.profile_id)
        if entry is None or entry[0] != ticket.generation:
            raise RuntimeError("readiness ticket is no longer active")
        try:
            await asyncio.wait_for(entry[1].event.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            if entry[1].outcome is None:
                self._counts[ReadinessOutcome.TIMEOUT] += 1
            raise
        if entry[1].outcome is None:
            raise RuntimeError("readiness notification is unavailable")
        return entry[1].outcome

    def finish(self, ticket: ReadinessTicket) -> None:
        entry = self._entries.get(ticket.profile_id)
        if entry is not None and entry[0] == ticket.generation:
            entry[1].active = False

    def health(self) -> dict[str, int]:
        return {
            "active": sum(1 for _generation, entry in self._entries.values() if entry.active),
            "success": self._counts[ReadinessOutcome.SUCCESS],
            "failure": self._counts[ReadinessOutcome.FAILURE],
            "timeout": self._counts[ReadinessOutcome.TIMEOUT],
        }


def _uses_passive_public_probe(profile: Any) -> bool:
    """Return whether relay health must avoid active public connections."""

    try:
        profile_id = ProfileId(getattr(profile, "id", None))
    except (TypeError, ValueError):
        return False
    return profile_id in _PASSIVE_PUBLIC_PROBE_PROFILES


def check_listening(protocol: str, port: int, *, connections: list[Any] | None = None) -> bool:
    """Check local listeners without treating UDP as a TCP LISTEN socket."""

    if protocol not in {"tcp", "udp"} or not isinstance(port, int) or not 1 <= port <= 65535:
        return False
    try:
        rows = connections if connections is not None else psutil.net_connections(kind=protocol)
    except (OSError, psutil.Error):
        return False
    for row in rows:
        try:
            address = row.laddr
            row_port = getattr(address, "port", address[1] if address else None)
            if row_port != port:
                continue
            if protocol == "tcp" and str(getattr(row, "status", "")).upper() == "LISTEN":
                return True
            if protocol == "udp":
                # UDP has no LISTEN state; an unconnected local endpoint is
                # represented by NONE on Linux (and may have no status field).
                status = str(getattr(row, "status", "")).upper()
                if status in {"", "NONE", "NONE "}:
                    return True
        except (AttributeError, IndexError, TypeError):
            continue
    return False


@dataclass(frozen=True)
class HealthResult:
    state: HealthState
    process_alive: bool
    ready: bool | None = None
    required_ports: bool | None = None
    relay: HealthState = HealthState.UNKNOWN
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def health(self) -> HealthState:
        return self.state


class HealthChecker:
    def __init__(
        self,
        adapter: Any | None = None,
        *,
        port_checker: Callable[[str, int], bool] = check_listening,
        relay_checker: Callable[[Any], bool | None | Awaitable[bool | None]] | None = None,
        public_probe: Callable[[str, int, str], bool | None | Awaitable[bool | None]] | None = None,
        process_checker: Callable[[Any, Any], bool] | None = None,
        process_validator: Callable[[Any, Any], bool] | None = None,
        a2s_checker: Callable[[Any], bool | None | Awaitable[bool | None]] | None = None,
    ):
        self.adapter = adapter
        self.port_checker = port_checker
        self.relay_checker = relay_checker
        self.public_probe = public_probe
        self.process_checker = process_checker or process_validator
        self.a2s_checker = a2s_checker
        # One successful marker per profile/process activation. A changed
        # started_at, pattern, or log path invalidates the entry naturally.
        self._ready_success: dict[str, tuple[str, str, tuple[str, ...]]] = {}

    async def check(
        self,
        profile: Any,
        *,
        observation: Any = _OBSERVATION_UNSET,
        connections: Mapping[str, list[Any]] | None = None,
        process_metrics: Any | None = None,
    ) -> HealthResult:
        if observation is _OBSERVATION_UNSET:
            try:
                observation = await self._observe(profile)
            except (AdapterError, RuntimeError):
                return HealthResult(state=HealthState.UNKNOWN, process_alive=False)
        # Adapter ``running`` is not a process identity proof (Crafty can be
        # alive while its controller Python process is the only process).
        process_alive = False
        evidence: list[str] = []
        if self.process_checker is not None:
            try:
                process_alive = bool(
                    self._call_with_connections(
                        self.process_checker,
                        profile,
                        observation,
                        connections=connections,
                        process_metrics=process_metrics,
                    )
                )
            except (OSError, ValueError, TypeError):
                process_alive = False
            if process_alive:
                evidence.append("validated-process")

        ports = tuple(getattr(profile, "ports", ()))
        required_ok: bool | None = None
        if ports:
            results: list[bool] = []
            for spec in ports:
                protocol = getattr(spec, "protocol", None)
                port = getattr(spec, "port", None)
                required = bool(getattr(spec, "required", True))
                result = bool(
                    self._call_with_connections(
                        self.port_checker,
                        protocol,
                        port,
                        connections=(connections or {}).get(protocol),
                    )
                )
                if required:
                    results.append(result)
                evidence.append(f"{protocol}:{port}:{'ok' if result else 'failed'}")
            required_ok = all(results) if results else True
        started_at = getattr(observation, "started_at", None)
        ready = self._ready_marker(profile, started_at)
        ready_pattern = getattr(getattr(profile, "process", None), "ready_log_pattern", None)
        if ready_pattern and ready is not True and await self._adapter_ready(profile, started_at):
            ready = True
        a2s: bool | None = None
        profile_id = getattr(getattr(profile, "id", None), "value", getattr(profile, "id", None))
        if profile_id == "pz-rising" and self.a2s_checker is not None and process_alive:
            try:
                value = self.a2s_checker(profile)
                a2s = await asyncio.wait_for(value, timeout=2.0) if inspect.isawaitable(value) else value
                if a2s is True:
                    evidence.append("a2s")
                elif a2s is False:
                    evidence.append("a2s:failed")
            except (OSError, asyncio.TimeoutError, ValueError, TypeError, RuntimeError):
                a2s = None
        adapter_healthy = getattr(observation, "healthy", None)
        # Do not infer health from process state.  Explicit adapter failure or
        # a failed required listener/ready marker is negative evidence.
        failed = adapter_healthy is False or required_ok is False or ready is False or a2s is False
        positive = adapter_healthy is True and (required_ok is not False) and (ready is not False)
        if ready is True:
            evidence.append("ready-marker")
        if failed:
            state = HealthState.UNHEALTHY
        elif process_alive and positive:
            state = HealthState.HEALTHY
        elif process_alive:
            state = HealthState.UNKNOWN
        else:
            state = HealthState.UNKNOWN

        relay = await self._relay(profile)
        return HealthResult(
            state=state,
            process_alive=process_alive,
            ready=ready,
            required_ports=required_ok,
            relay=relay,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _call_with_connections(
        function: Callable[..., Any],
        *args: Any,
        connections: Any,
        process_metrics: Any | None = None,
    ) -> Any:
        try:
            parameters = signature_parameters(function)
            accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            supported = {parameter.name for parameter in parameters}
        except (TypeError, ValueError):
            accepts_kwargs = True
            supported = set()
        optional = {"connections": connections}
        if process_metrics is not None:
            optional["process_metrics"] = process_metrics
        filtered = optional if accepts_kwargs else {
            key: value for key, value in optional.items() if key in supported
        }
        return function(*args, **filtered)

    async def _observe(self, profile: Any) -> Any:
        if self.adapter is None:
            return type("Observation", (), {"running": False, "healthy": None})()
        value = self.adapter.observe(profile)
        return await value if inspect.isawaitable(value) else value

    def _ready_marker(self, profile: Any, started_at: Any = None) -> bool | None:
        spec = getattr(getattr(profile, "process", None), "ready_log_pattern", None)
        if not spec:
            # Crafty's int_ping/actual JVM evidence is its readiness signal.
            return None
        paths = getattr(getattr(profile, "paths", None), "log_files", ())
        if not paths:
            return None
        raw_profile_id = getattr(getattr(profile, "id", None), "value", getattr(profile, "id", None))
        cache_key: str | None = None
        cache_value: tuple[str, str, tuple[str, ...]] | None = None
        if isinstance(raw_profile_id, str) and isinstance(started_at, datetime):
            cache_key = raw_profile_id
            cache_value = (started_at.isoformat(), str(spec), tuple(str(path) for path in paths))
            if self._ready_success.get(cache_key) == cache_value:
                return True
        for raw_path in paths:
            path = Path(raw_path)
            try:
                if not path.is_file():
                    continue
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - _MAX_READY_BYTES))
                    data = stream.read(_MAX_READY_BYTES)
                if size > _MAX_READY_BYTES:
                    _, _, data = data.partition(b"\n")
                text = data.decode("utf-8", "replace")
                for line in text.splitlines():
                    if spec not in line:
                        continue
                    if started_at is None:
                        continue
                    if started_at is not None:
                        prefix = line.strip().split(maxsplit=1)[0]
                        try:
                            parsed = datetime.fromisoformat(prefix.replace("Z", "+00:00"))
                            if parsed.tzinfo is None:
                                parsed = parsed.replace(tzinfo=timezone.utc)
                            if started_at.tzinfo is None:
                                started_at = started_at.replace(tzinfo=timezone.utc)
                            if parsed < started_at:
                                continue
                        except (AttributeError, TypeError, ValueError):
                            continue
                    if cache_key is not None and cache_value is not None:
                        self._ready_success[cache_key] = cache_value
                    return True
            except (OSError, UnicodeError):
                continue
        return False

    async def _adapter_ready(self, profile: Any, started_at: Any) -> bool:
        if (
            started_at is None
            or not isinstance(started_at, datetime)
            or started_at.tzinfo is None
            or self.adapter is None
            or not hasattr(self.adapter, "recent_logs")
        ):
            return False
        try:
            value = self.adapter.recent_logs(profile, 5000)
            lines = await value if inspect.isawaitable(value) else value
        except (OSError, asyncio.TimeoutError, ValueError, TypeError, RuntimeError):
            return False
        for line in lines if isinstance(lines, (list, tuple)) else ():
            timestamp = getattr(line, "timestamp", None)
            message = getattr(line, "message", "")
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                continue
            if timestamp >= started_at and isinstance(message, str):
                if getattr(getattr(profile, "process", None), "ready_log_pattern", "") in message:
                    return True
        return False

    async def _relay(self, profile: Any) -> HealthState:
        endpoint = getattr(profile, "public_endpoint", None)
        if endpoint is None:
            return HealthState.UNKNOWN
        local: bool | None = None
        if self.relay_checker is not None:
            try:
                value = self.relay_checker(endpoint)
                local = await value if inspect.isawaitable(value) else value
            except (OSError, asyncio.TimeoutError, ValueError):
                local = False
        if local is False:
            return HealthState.UNHEALTHY
        if _uses_passive_public_probe(profile):
            # Terraria's public TCP endpoint is the game protocol itself;
            # connecting here consumes a player slot.  Relay reachability is
            # therefore unknown unless a separate, passive signal is added.
            return HealthState.UNKNOWN
        public: bool | None = None
        if self.public_probe is not None:
            try:
                value = self.public_probe(endpoint.host, endpoint.port, endpoint.protocol)
                public = await asyncio.wait_for(value, timeout=2.0) if inspect.isawaitable(value) else value
            except (OSError, asyncio.TimeoutError, ValueError):
                public = None
        elif endpoint.protocol == "tcp":
            public = await self._tcp_probe(endpoint.host, endpoint.port)
        # UDP cannot be proven by a blind datagram; unknown is safer than
        # claiming a healthy public endpoint.
        if local is True and public is True:
            return HealthState.HEALTHY
        if public is False:
            return HealthState.UNHEALTHY
        return HealthState.UNKNOWN

    @staticmethod
    async def _tcp_probe(host: str, port: int) -> bool | None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False


tcp_listening = lambda port: check_listening("tcp", port)
udp_listening = lambda port: check_listening("udp", port)

__all__ = ["HealthResult", "HealthChecker", "check_listening", "tcp_listening", "udp_listening"]
