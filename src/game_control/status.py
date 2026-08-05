"""Public state derivation and status snapshots."""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

import psutil

from .adapters.base import AdapterError
from .models import HealthState, ObservedState
from .protocol import ProfileStatus, StatusSnapshot


def derive_state(
    *,
    active_job: str | None,
    process_alive: bool,
    conflicting_slot_owner: bool | str | None,
) -> ObservedState:
    """Apply the closed state precedence contract.

    Process state is independent from health: a live process remains RUNNING
    even if its health adapter reports UNHEALTHY.
    """

    job = getattr(active_job, "value", active_job)
    if job == "start":
        return ObservedState.STARTING
    if job == "stop":
        return ObservedState.STOPPING
    if job == "failed":
        return ObservedState.FAILED
    if process_alive:
        return ObservedState.RUNNING
    if conflicting_slot_owner:
        return ObservedState.BLOCKED
    return ObservedState.STOPPED


class StatusService:
    def __init__(
        self,
        profiles: Iterable[Any],
        *,
        adapters: Mapping[Any, Any] | None = None,
        adapter: Any | None = None,
        slot_observer: Callable[[], Any] | None = None,
        active_jobs: Mapping[Any, str | None] | Callable[[Any], str | None] | None = None,
        health_checker: Any | None = None,
        metrics: Any | None = None,
        player_tracker: Any | None = None,
        session_store: Any | None = None,
        connection_provider: Callable[..., Any] = psutil.net_connections,
        generation: int | Callable[[], int] = 0,
        clock: Callable[[], datetime] | None = None,
    ):
        self.profiles = tuple(profiles)
        self.adapters = adapters or {}
        self.default_adapter = adapter
        self.slot_observer = slot_observer
        self.active_jobs = active_jobs or {}
        self.health_checker = health_checker
        self.metrics = metrics
        self.player_tracker = player_tracker
        self.session_store = session_store
        self.connection_provider = connection_provider
        self.generation = generation
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_jobs: dict[Any, str | None] = {}
        self._last_running: dict[str, bool] = {}
        self._version_cache: dict[str, tuple[tuple[int, int], str | None]] = {}
        self._snapshot_cache: StatusSnapshot | None = None
        self._refresh_lock = asyncio.Lock()

    async def snapshot(self) -> StatusSnapshot:
        """Refresh and publish the projection used by all status reads."""
        async with self._refresh_lock:
            snapshot = await self._sample()
            self._snapshot_cache = snapshot
            return snapshot

    async def cached_snapshot(self) -> StatusSnapshot:
        """Return the last sampled projection without running probes."""
        cached = self._snapshot_cache
        if cached is not None:
            return cached
        return await self.snapshot()

    async def _sample(self) -> StatusSnapshot:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        slot = await self._call(self.slot_observer) if self.slot_observer else None
        owner = getattr(slot, "owner", None)
        owner_value = getattr(owner, "value", owner)
        contexts = []
        for profile in self.profiles:
            profile_id = getattr(profile, "id", None)
            key = getattr(profile_id, "value", profile_id)
            job = self._job_for(profile_id, key)
            full_probe = (
                self.slot_observer is None
                or job is not None
                or bool(owner_value and owner_value == key)
            )
            contexts.append((profile, profile_id, key, job, full_probe))
        invalidate = getattr(self.metrics, "invalidate_cgroup", None)
        if callable(invalidate):
            for profile, _profile_id, key, job, _full_probe in contexts:
                previous_job = self._last_jobs.get(key, object())
                if previous_job != job and job in {"start", "stop"}:
                    invalidate(getattr(profile, "systemd_unit", None))
                self._last_jobs[key] = job
        connections = self._connections_for_snapshot(
            profile for profile, _profile_id, _key, _job, full_probe in contexts if full_probe
        )
        statuses: list[ProfileStatus] = []
        for profile, profile_id, key, job, full_probe in contexts:
            conflicting = bool(owner_value and owner_value != key)
            if not full_probe:
                cached_disk = None
                cached_disk_provider = getattr(self.metrics, "cached_disk_metrics", None) if self.metrics is not None else None
                if callable(cached_disk_provider):
                    cached_disk = await self._call(cached_disk_provider, profile)
                statuses.append(
                    ProfileStatus(
                        profile_id=profile_id,
                        state=derive_state(
                            active_job=job,
                            process_alive=False,
                            conflicting_slot_owner=conflicting,
                        ),
                        health=HealthState.UNKNOWN,
                        slot_owner=owner if owner_value else None,
                        active_job_id=job,
                        pid=None,
                        started_at=None,
                        uptime_seconds=None,
                        cpu_percent=None,
                        rss_bytes=None,
                        players_online=None,
                        installed_version=self._cached_installed_version(profile),
                        restart_required=False,
                        required_ports_ready=False,
                        disk_free_bytes=getattr(cached_disk, "profile_data_free_bytes", None),
                        disk_read_bps=None,
                        disk_write_bps=None,
                    )
                )
                continue
            adapter = self.adapters.get(profile_id, self.adapters.get(key, self.default_adapter))
            observation_error = False
            try:
                observation = await self._observe(adapter, profile)
            except (AdapterError, RuntimeError):
                observation_error = True
                observation = type("Observation", (), {"running": False, "healthy": None})()
            running = bool(getattr(observation, "running", False))
            if not observation_error and not running:
                if self.session_store is not None and self._last_running.get(key, False):
                    self.session_store.profile_stopped(key, now=_iso(now))
                if self.player_tracker is not None:
                    reset = getattr(self.player_tracker, "reset", None)
                    if callable(reset):
                        reset(key)
            self._last_running[key] = running if not observation_error else self._last_running.get(key, False)
            process_alive = False
            health = None
            required_ports = getattr(observation, "required_ports_ready", None)
            health_error = False
            if self.health_checker is not None and not observation_error:
                checker = self.health_checker
                if isinstance(checker, Mapping):
                    checker = checker.get(profile_id, checker.get(key))
                if checker is not None:
                    try:
                        result = await self._call_optional(
                            checker.check, profile, connections=connections
                        )
                    except (AdapterError, RuntimeError):
                        health_error = True
                    else:
                        health = getattr(result, "state", result)
                        required_ports = getattr(result, "required_ports", required_ports)
                        validated_alive = getattr(result, "process_alive", None)
                        if isinstance(validated_alive, bool):
                            process_alive = validated_alive
            state = derive_state(
                active_job=job,
                process_alive=process_alive,
                conflicting_slot_owner=conflicting,
            )
            if health is None:
                observed_health = None if observation_error or health_error else getattr(observation, "healthy", None)
                health = (
                    HealthState.HEALTHY
                    if observed_health is True
                    else HealthState.UNHEALTHY
                    if observed_health is False
                    else HealthState.UNKNOWN
                )
            health = HealthState(getattr(health, "value", health))
            sampled = None
            if self.metrics is not None:
                sampled = await self._call_optional(
                    self.metrics.sample,
                    profile,
                    pid=getattr(observation, "pid", None),
                    connections=connections,
                )
            pid = getattr(sampled, "pid", None)
            rss = getattr(sampled, "rss_bytes", None)
            cpu = getattr(sampled, "cpu_percent", None)
            started_at = getattr(observation, "started_at", None)
            uptime = None
            if started_at is not None:
                try:
                    uptime = max(0, int((now - started_at).total_seconds()))
                except (TypeError, ValueError):
                    uptime = None
            players = getattr(observation, "players_online", None)
            if players is None and self.player_tracker is not None:
                players = await self._call(
                    self.player_tracker.count,
                    profile,
                    adapter,
                    running=bool(getattr(observation, "running", False)),
                )
            if running and self.session_store is not None:
                names = getattr(observation, "player_names", None)
                if names is None and self.player_tracker is not None:
                    names = self.player_tracker.names(key)
                source = "crafty" if getattr(getattr(profile, "adapter", None), "value", getattr(profile, "adapter", None)) == "crafty" else "log"
                self.session_store.record(
                    key,
                    set(names) if names is not None else None,
                    players,
                    now=_iso(now),
                    source=source,
                )
            version = getattr(observation, "installed_version", None)
            if version is None:
                version = self._cached_installed_version(profile)
            statuses.append(
                ProfileStatus(
                    profile_id=profile_id,
                    state=state,
                    health=health,
                    slot_owner=owner if owner_value else None,
                    active_job_id=job,
                    pid=pid,
                    started_at=started_at,
                    uptime_seconds=uptime,
                    cpu_percent=cpu,
                    rss_bytes=rss,
                    players_online=players,
                    installed_version=version,
                    restart_required=False,
                    required_ports_ready=bool(required_ports),
                    disk_free_bytes=getattr(getattr(sampled, "disk", None), "profile_data_free_bytes", None),
                    disk_read_bps=getattr(sampled, "disk_read_bps", None),
                    disk_write_bps=getattr(sampled, "disk_write_bps", None),
                )
            )
        generation = self.generation() if callable(self.generation) else self.generation
        return StatusSnapshot(generation=int(generation), observed_at=now, profiles=tuple(statuses))

    def _job_for(self, profile_id: Any, key: Any) -> str | None:
        if callable(self.active_jobs):
            return self.active_jobs(profile_id)
        value = self.active_jobs.get(profile_id, self.active_jobs.get(key))
        if isinstance(value, Mapping):
            value = value.get("operation", value.get("state"))
        return getattr(value, "value", value)

    def _cached_installed_version(self, profile: Any) -> str | None:
        direct = getattr(profile, "installed_version", None)
        if isinstance(direct, str) and direct:
            return direct
        paths = getattr(profile, "paths", None)
        raw_path = getattr(paths, "version_file", None) if paths is not None else None
        if raw_path is None:
            return None
        path = Path(raw_path)
        key = str(path)
        try:
            stat = path.stat()
            fingerprint = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            return None
        cached = self._version_cache.get(key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        try:
            value = path.read_text(encoding="utf-8").strip() or None
        except OSError:
            value = None
        self._version_cache[key] = (fingerprint, value)
        return value

    async def _observe(self, adapter: Any, profile: Any) -> Any:
        if adapter is None:
            return type("Observation", (), {"running": False, "healthy": None})()
        return await self._call(adapter.observe, profile)

    def _connections_for_snapshot(self, profiles: Iterable[Any]) -> dict[str, list[Any]]:
        protocols = {
            getattr(spec, "protocol", None)
            for profile in profiles
            for spec in tuple(getattr(profile, "ports", ()))[:16]
            if getattr(spec, "protocol", None) in {"tcp", "udp"}
        }
        rows: dict[str, list[Any]] = {}
        for protocol in ("tcp", "udp"):
            if protocol not in protocols:
                continue
            try:
                value = self.connection_provider(kind=protocol)
                rows[protocol] = list(value)
            except (OSError, psutil.Error, TypeError, ValueError):
                rows[protocol] = []
        return rows

    async def _call_optional(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            parameters = inspect.signature(function).parameters.values()
            accepts_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
            supported = {parameter.name for parameter in parameters}
        except (TypeError, ValueError):
            accepts_kwargs = True
            supported = set()
        filtered = kwargs if accepts_kwargs else {key: value for key, value in kwargs.items() if key in supported}
        return await self._call(function, *args, **filtered)

    @staticmethod
    async def _call(function: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
        if function is None:
            return None
        value = function(*args, **kwargs)
        return await value if inspect.isawaitable(value) else value


derive_observed_state = derive_state

__all__ = ["StatusService", "derive_state", "derive_observed_state"]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
