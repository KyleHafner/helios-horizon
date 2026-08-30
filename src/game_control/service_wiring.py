"""Closed production facades for controller service seams.

The controller deals in RPC protocol models while the task services expose
domain records and profile-oriented methods.  These adapters keep that
translation in one root-owned module; no RPC value is used as a path, command,
URL, or credential.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import inspect
import sqlite3
import math
import time
import urllib.request
import urllib.parse
import os
import stat
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from .backups import B2CommandTransport, B2ProtectionService, BackupService, RestoreService
from .alert_policy import PerformanceAlertEvaluator
from .benchmark_safety import BenchmarkPreflight, prometheus_ups_provider
from .benchmarks import BenchmarkService, parse_benchmark_plans
from .capability_evidence import RootWakeSafetyEvidence
from .errors import SafeError
from .health import HealthChecker
from .logs import LogService
from .metrics import MetricSampler
from .players import PlayerTracker
from .metrics import HostTelemetrySource
from .session_store import SessionStore
from .tps import FIXED_EXPORTER_URL, TpsSampler
from .telemetry_db import TelemetryDatabase
from .state_db import STATE_DB_PATH
from .telemetry_sampler import TelemetrySampler
from .stats_queries import stats_tps as query_stats_tps
from .tick_telemetry import ExporterRegistry, ExporterSpec, PrometheusTickParser, TickTelemetry
from .rcon_telemetry import PerformanceResult, PersistentRconTelemetry, PlayerCountResult, TelemetryCommand
from .log_follower import LogFollower
from .gc_telemetry import GcTelemetryParser
from .models import BackupDestination, NotificationEvent, ProfileId
from .notifications import DEFAULT_SECRET_DIR, NotificationService
from .protocol import (
    AuditPage,
    AuditSummary,
    BackupPage,
    BackupSummary,
    EventPage,
    EventSummary,
    ErrorCode,
    GetProfiles,
    JobAccepted,
    LogPage,
    NotificationConfig,
    ProfileStatus,
    PublicProfile,
    PublicEndpoint,
    UpdateStatus,
)
from .redaction import Redactor, SecretRegistry
from .root_state import RootActiveJobsReader, RootGenerationReader
from .status import StatusService
from .updates import UpdateService
from .worlds import WorldService
from .runtime.telemetry import (
    DEFAULT_HOST_METRICS,
    ResourceRef,
    TelemetryCollector,
    TelemetryRuntimeConfig,
)


SUNLIT_ONLINE_SNAPSHOT_SECONDS = 240.0
_HOST_TELEMETRY_METRICS = DEFAULT_HOST_METRICS

# Controller-facing history pages are read frequently and can contain enough
# rows to make sqlite3.fetchall() visible on the event loop.  Keep a small,
# shared pool so concurrent readers are bounded instead of creating an
# unbounded thread per RPC.  Production StateDatabase instances are reopened
# per worker, keeping sqlite connections thread-local.
_AUDIT_QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="horizon-audit-query"
)
_AUDIT_STATE_DB_PATH = STATE_DB_PATH


def _key(value: Any) -> str:
    value = getattr(value, "id", value)
    value = getattr(value, "value", value)
    return str(value)


def _connection(database: Any) -> sqlite3.Connection | Any | None:
    value = getattr(database, "connection", database)
    return value if hasattr(value, "execute") else None


def _isolated_database(database: Any) -> Any | None:
    """Open a worker-local state DB; never move the event-loop connection."""
    path = getattr(database, "path", None)
    opener = getattr(type(database), "open", None)
    if path is None or not callable(opener):
        return None
    try:
        return opener(path)
    except Exception:
        return None


def _close_database(database: Any | None) -> None:
    if database is not None and hasattr(database, "close"):
        database.close()


_prometheus_ups_provider = prometheus_ups_provider


class _StatusFacade:
    def __init__(self, service: StatusService):
        self.service = service

    async def snapshot(
        self,
        action: Any = None,
        actor: str | None = None,
        request_id: Any = None,
        *,
        maintenance: bool = False,
    ):
        # The dedicated slotd sampler owns the 5-second persistence cadence.
        # Maintenance still needs a fresh projection for controller decisions,
        # but must never become a second telemetry writer.
        return await self.service.snapshot(
            persist=False,
            force=bool(maintenance or getattr(action, "refresh", False)),
        )

    async def cached_snapshot(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return await self.service.cached_snapshot()

    def telemetry_health(self) -> dict[str, Any]:
        return self.service.telemetry_health()

    async def benchmark_eligibility(
        self,
        *,
        maintenance_window: bool,
        rollback_safe: bool,
        public_wake_policy: str,
        snapshot: Any = None,
    ):
        return await self.service.benchmark_eligibility(
            maintenance_window=maintenance_window,
            rollback_safe=rollback_safe,
            public_wake_policy=public_wake_policy,
            snapshot=snapshot,
        )


def _legacy_telemetry_config(stats: Mapping[str, Any], approved_profile_ids: tuple[str, ...]):
    """Translate the pre-runtime stats shape at this temporary boundary only."""
    if not isinstance(stats, Mapping):
        raise ValueError("legacy telemetry settings are invalid")
    allowed = {
        "exporter_url", "tick_profile", "log_checkpoint_dir", "gc_log_path",
        "gc_profile_id", "host_metrics", "legacy_tps_mode", "legacy_tps_interval_seconds",
        "tps_interval_seconds",
    }
    if set(stats) - allowed:
        raise ValueError("legacy telemetry settings contain unknown keys")
    approved = tuple(str(getattr(item, "value", item)) for item in approved_profile_ids)
    settings: dict[str, Any] = {
        "legacy_tps_mode": stats.get("legacy_tps_mode", "disabled"),
    }
    for key in ("log_checkpoint_dir", "host_metrics", "legacy_tps_interval_seconds"):
        if key in stats:
            settings[key] = stats[key]
    if "legacy_tps_interval_seconds" not in settings and "tps_interval_seconds" in stats:
        settings["legacy_tps_interval_seconds"] = stats["tps_interval_seconds"]
    exporter_url = stats.get("exporter_url")
    if exporter_url is not None:
        tick_profile = stats.get("tick_profile")
        if tick_profile is None and ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value in approved:
            tick_profile = ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
        tick_profile = str(getattr(tick_profile, "value", tick_profile))
        if tick_profile not in approved:
            raise ValueError("legacy tick profile is not in the approved root registry")
        settings.update(exporter_url=exporter_url, tick_profile=tick_profile)
    gc_profile = stats.get("gc_profile_id")
    gc_path = stats.get("gc_log_path")
    if gc_profile is None and gc_path is None and stats.get("log_checkpoint_dir") and ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value in approved:
        # Temporary compatibility default for the pre-binding production TOML.
        gc_profile = ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
        gc_path = "/srv/game-servers/minecraft-sunlit-cobblemon/logs/gc.log"
    if gc_profile is not None or gc_path is not None:
        settings.update(gc_profile_id=str(getattr(gc_profile, "value", gc_profile)), gc_log_path=gc_path)
    return TelemetryRuntimeConfig.from_root_config(settings, approved_profile_ids=approved)


class _LegacyAlertSink:
    def __init__(self, target: Any):
        self.target = target

    def observe(self, observation: Any) -> None:
        self.target.observe(
            observation.profile_id,
            profile_state=observation.profile_state,
            now=observation.now,
            mspt_p95=observation.mspt_p95,
            rss_bytes=observation.rss_bytes,
            wake_duration_ms=observation.wake_duration_ms,
            benchmark_regression=observation.benchmark_regression,
        )


class _BoundTelemetryCollectors(TelemetryCollector):
    """Compatibility adapter retaining the pre-runtime collector call shape."""

    def __init__(self, *, profiles: tuple[Any, ...], database: Any, stats: Mapping[str, Any],
                 rcon: PersistentRconTelemetry | Any | None, player_tracker: PlayerTracker,
                 alerts: Any | None = None):
        self._legacy_alerts = alerts
        config = _legacy_telemetry_config(stats, tuple(_key(profile) for profile in profiles))
        super().__init__(profiles=profiles, config=config, database=database, rcon=rcon,
                         player_tracker=player_tracker,
                         alert_sink=None if alerts is None else _LegacyAlertSink(alerts))

    @property
    def alerts(self) -> Any | None:
        return self._legacy_alerts

    @alerts.setter
    def alerts(self, value: Any | None) -> None:
        self._legacy_alerts = value
        self.alert_sink = None if value is None else _LegacyAlertSink(value)

    async def collect(self, *, running: Mapping[str, Any]) -> None:
        statuses = []
        for profile in self.profiles:
            profile_id = _key(profile)
            value = running.get(profile_id, False)
            if isinstance(value, bool):
                statuses.append(type("LegacyStatus", (), {
                    "profile_id": profile_id,
                    "state": "running" if value else "stopped",
                    "pid": None,
                    "rss_bytes": None,
                })())
            else:
                statuses.append(value)
        snapshot = type("LegacySnapshot", (), {"profiles": tuple(statuses)})()
        await super().collect(snapshot)

    async def close(self) -> None:
        await super().close()
        close = getattr(self._legacy_alerts, "close", None)
        if callable(close):
            await close()



class _PerformanceAlerts:
    """Bounded bridge from fixed evaluations to notification de-duplication."""

    def __init__(self, profiles: Mapping[str, Any], notifications: NotificationService, *, max_pending: int = 8):
        self.profiles = profiles
        self.notifications = notifications
        self.evaluator = PerformanceAlertEvaluator()
        self.max_pending = max(1, min(32, int(max_pending)))
        self._pending: set[asyncio.Task[Any]] = set()
        self.dropped = 0
        self.failures = 0

    def observe(self, profile_id: Any, **values: Any) -> None:
        key = _key(profile_id)
        for emission in self.evaluator.observe(key, **values):
            # Disk pressure can never arrive here: the evaluator emits only
            # rules owned by slotd.
            if emission.signal.value not in {
                "sustained_mspt", "memory_growth", "wake_slo", "benchmark_regression"
            }:
                continue
            if len(self._pending) >= self.max_pending:
                self.dropped += 1
                continue
            task = asyncio.create_task(
                self.notifications.send_async(
                    self.profiles[key], NotificationEvent(emission.signal.value),
                    emission.generation, emission.message,
                ),
                name=f"horizon-alert-{emission.signal.value}",
            )
            self._pending.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        try:
            task.result()
        except Exception:
            self.failures += 1

    async def close(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)


class _LogsFacade:
    def __init__(self, profiles: Mapping[str, Any], adapters: Mapping[Any, Any], redactor: Redactor):
        self.profiles = profiles
        self.adapters = adapters
        self.redactor = redactor
        self._services = {
            key: LogService(adapters.get(key) or adapters.get(profile.id), redactor=redactor)
            for key, profile in profiles.items()
        }

    async def page(self, action: Any, actor: str | None = None, request_id: Any = None) -> LogPage:
        key = _key(action.profile_id)
        try:
            profile = self.profiles[key]
            service = self._services[key]
        except KeyError as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc
        options = action.page
        lines = await service.tail(
            profile,
            limit=options.limit,
            since=options.since,
            until=options.until,
        )
        severity = getattr(options, "severity", "all")
        if severity != "all":
            lines = tuple(line for line in lines if line.severity == severity)
        return LogPage(items=tuple(lines), next_cursor=None)

    async def tail(self, profile: Any, *, limit: int = 500, since: datetime | None = None):
        key = _key(profile)
        return await self._services[key].tail(self.profiles[key], limit=limit, since=since)

    async def search(self, profile: Any, query: str, *, limit: int = 100, regex: bool = False):
        key = _key(profile)
        return await self._services[key].search(self.profiles[key], query, limit=limit, regex=regex)


class _BackupFacade:
    def __init__(
        self,
        profiles: Mapping[str, Any],
        adapters: Mapping[Any, Any],
        database: Any,
        *,
        b2_transport: Any | None = None,
        sunlit_online_backup: Any | None = None,
        telemetry_db: Any | None = None,
    ):
        self.profiles = profiles
        self.adapters = adapters
        self.database = database
        self.b2_transport = b2_transport or B2CommandTransport()
        self.sunlit_online_backup = sunlit_online_backup
        self.protection = B2ProtectionService(database=database, transport=self.b2_transport)
        self._profile_locks: dict[str, asyncio.Lock] = {
            key: asyncio.Lock() for key in profiles
        }
        self.services = {
            key: BackupService(
                profile,
                database=database,
                stopped_check=lambda profile=profile: self._stopped_sync(profile),
                protection_service=self.protection,
                telemetry_db=telemetry_db,
            )
            for key, profile in profiles.items()
        }
        self.restores = {
            key: RestoreService(
                profile,
                backup_service=self.services[key],
                stopped_check=lambda profile=profile: self._stopped_sync(profile),
            )
            for key, profile in profiles.items()
        }

    def _service(self, profile_id: Any) -> tuple[Any, BackupService]:
        key = _key(profile_id)
        try:
            return self.profiles[key], self.services[key]
        except KeyError as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc

    async def _stopped(self, profile: Any) -> None:
        adapter = self.adapters.get(profile.id) or self.adapters.get(_key(profile))
        if adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("profile_unavailable", "profile state could not be proven")
        try:
            value = adapter.observe(profile)
            observation = await value if inspect.isawaitable(value) else value
        except Exception as exc:
            raise SafeError("profile_unavailable", "profile state could not be proven") from exc
        if bool(getattr(observation, "running", False)):
            raise SafeError("profile_running", "profile is running; it must be stopped before backup")

    def _stopped_sync(self, profile: Any) -> bool:
        """Revalidate the authoritative adapter state in the worker thread."""
        adapter = self.adapters.get(profile.id) or self.adapters.get(_key(profile))
        if adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("profile_unavailable", "profile state could not be proven")
        try:
            value = adapter.observe(profile)
            if inspect.isawaitable(value):
                value = asyncio.run(value)
        except SafeError:
            raise
        except Exception as exc:
            raise SafeError("profile_unavailable", "profile state could not be proven") from exc
        if bool(getattr(value, "running", False)):
            raise SafeError("profile_running", "profile is running; it must be stopped before backup")
        return True

    async def list(self, action: Any, actor: str | None = None, request_id: Any = None) -> BackupPage:
        profile, service = self._service(action.profile_id)
        result = service.list(action, actor, request_id)
        if isinstance(result, BackupPage):
            return result
        items = []
        for record in result:
            created = getattr(record, "created_at", None)
            items.append(
                BackupSummary(
                    id=str(getattr(record, "id", "")),
                    profile_id=profile.id,
                    created_at=_timestamp(created),
                    size_bytes=max(0, int(getattr(record, "size_bytes", 0))),
                    verified=bool(getattr(record, "verified", False)),
                    protected=bool(getattr(record, "protected", False)),
                )
            )
        return BackupPage(items=tuple(items), next_cursor=None)

    async def create(self, action: Any, actor: str | None = None, request_id: Any = None, lease_check: Any = None) -> JobAccepted:
        profile, service = self._service(action.profile_id)
        lock = self._profile_locks.setdefault(_key(profile), asyncio.Lock())
        async with lock:
            adapter = self.adapters.get(profile.id) or self.adapters.get(_key(profile))
            if adapter is None or not hasattr(adapter, "observe"):
                raise SafeError("profile_unavailable", "profile state could not be proven")
            try:
                observed = adapter.observe(profile)
                observed = await observed if inspect.isawaitable(observed) else observed
            except Exception as exc:
                raise SafeError("profile_unavailable", "profile state could not be proven") from exc
            online = bool(getattr(observed, "running", False))
            sunlit = _key(profile) == ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
            if online and (not sunlit or self.sunlit_online_backup is None):
                raise SafeError("profile_running", "profile is running; it must be stopped before backup")
            if not online:
                await self._stopped(profile)

            def work():
                worker_db = _isolated_database(self.database)
                if action.destination is BackupDestination.HORIZON_B2 and worker_db is None:
                    raise SafeError(
                        "backup_protection_failed",
                        "durable backup state is unavailable",
                    )
                stopped_check = lambda: self._stopped_sync(profile)
                worker = BackupService(
                    profile,
                    database=worker_db,
                    stopped_check=stopped_check,
                    free_space=service.free_space,
                    clock=service.clock,
                    tar_runner=service.tar_runner,
                    protection_service=B2ProtectionService(
                        database=worker_db,
                        transport=self.b2_transport,
                        clock=service.clock,
                    ),
                    lease_check=lease_check,
                )
                try:
                    if online:
                        worker.online_transport = self.sunlit_online_backup
                        result = worker.create_online(
                            action,
                            actor,
                            request_id,
                            protected=bool(action.protected),
                            max_snapshot_seconds=SUNLIT_ONLINE_SNAPSHOT_SECONDS,
                        )
                    else:
                        result = worker.create(
                            action,
                            actor,
                            request_id,
                            protected=bool(action.protected),
                        )
                    return result, worker_db is not None
                finally:
                    _close_database(worker_db)

            record, isolated = await asyncio.to_thread(work)
            if not isolated:
                service._insert(record)
            return JobAccepted(job_id=getattr(record, "id", uuid4().hex), state="running")

    def protect(self, profile_id: Any, backup_id: str, protected: bool = True):
        _profile, service = self._service(profile_id)
        return service.protect(backup_id, protected)

    async def confirm_restore(
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None,
        lease_check: Callable[[], bool] | None = None,
    ) -> JobAccepted:
        payload = payload or {}
        profile_id = payload.get("profile_id")
        if profile_id is None:
            profile_id = getattr(action, "profile_id", None)
        profile, _service = self._service(profile_id)
        await self._stopped(profile)
        backup_id = str(payload.get("backup_id", ""))
        if not backup_id or "/" in backup_id or "\\" in backup_id or backup_id in {".", ".."}:
            raise SafeError("invalid_backup", "backup archive is not approved")
        archive = Path(profile.paths.backup_root) / f"{backup_id}.tar.zst"
        source_backup = self.services[_key(profile.id)]
        source_restore = self.restores[_key(profile.id)]
        def work():
            worker_db = _isolated_database(self.database)
            worker_backup = BackupService(
                profile,
                database=worker_db,
                stopped_check=lambda: self._stopped_sync(profile),
                free_space=source_backup.free_space,
                clock=source_backup.clock,
                tar_runner=source_backup.tar_runner,
                lease_check=lease_check,
            )
            worker_restore = RestoreService(
                profile,
                backup_service=worker_backup,
                stopped_check=lambda: self._stopped_sync(profile),
                free_space=source_restore.free_space,
                health_check=None,
                lease_check=lease_check,
            )
            try:
                result = worker_restore.restore(archive, actor, request_id)
                destinations = result.destinations or (result.destination,)
                healthy = all(destination.is_dir() for destination in destinations)
                if source_restore.health_check is not None:
                    healthy = all(bool(source_restore.health_check(destination)) for destination in destinations)
                if healthy:
                    worker_restore.finalize(result)
                else:
                    worker_restore.rollback(result)
                    raise SafeError("restore_health_failed", "restored profile failed health validation")
                return result
            finally:
                _close_database(worker_db)

        result = await asyncio.to_thread(work)
        return JobAccepted(job_id=getattr(result, "backup_id", uuid4().hex), state="running")

    restore = confirm_restore

    def reconcile_startup(self) -> None:
        for restore in self.restores.values():
            restore.reconcile()


class _WorldFacade:
    def __init__(self, service: WorldService, profiles: Mapping[str, Any], adapters: Mapping[Any, Any]):
        self.service = service
        self.profiles = profiles
        self.adapters = adapters

    async def _stopped(self) -> None:
        for profile_id in (ProfileId.TERRARIA_VANILLA.value, ProfileId.TERRARIA_TMOD.value):
            profile = self.profiles.get(profile_id)
            adapter = self.adapters.get(profile.id) if profile is not None else None
            if profile is None or adapter is None or not hasattr(adapter, "observe"):
                raise SafeError("profile_unavailable", "profile state could not be proven")
            value = adapter.observe(profile)
            observation = await value if inspect.isawaitable(value) else value
            if bool(getattr(observation, "running", False)):
                raise SafeError("profile_running", "profiles must be stopped before clone")

    def _stopped_sync_pair(self) -> bool:
        for profile_id in (ProfileId.TERRARIA_VANILLA.value, ProfileId.TERRARIA_TMOD.value):
            profile = self.profiles.get(profile_id)
            adapter = self.adapters.get(profile.id) if profile is not None else None
            if profile is None or adapter is None or not hasattr(adapter, "observe"):
                raise SafeError("profile_unavailable", "profile state could not be proven")
            value = adapter.observe(profile)
            if inspect.isawaitable(value):
                value = asyncio.run(value)
            if bool(getattr(value, "running", False)):
                raise SafeError("profile_running", "profiles must be stopped before clone")
        return True

    async def confirm_clone(
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None,
        lease_check: Callable[[], bool] | None = None,
    ) -> JobAccepted:
        payload = payload or {}
        await self._stopped()
        source_backup = self.service.backup_service
        def work():
            worker_db = _isolated_database(source_backup.database)
            worker_backup = BackupService(
                self.service.vanilla,
                database=worker_db,
                stopped_check=lambda: self._stopped_sync(self.service.vanilla),
                free_space=source_backup.free_space,
                clock=source_backup.clock,
                tar_runner=source_backup.tar_runner,
            )
            worker_service = WorldService(
                self.service.vanilla,
                self.service.tmod,
                backup_service=worker_backup,
                stopped_check=lambda *_: self._stopped_sync_pair(),
                clock=self.service.clock,
                lease_check=lease_check,
            )
            try:
                result = worker_service.clone_vanilla_to_tmod(
                    str(payload.get("source_world_id", "")),
                    str(payload.get("destination_name", "")),
                    actor,
                    request_id,
                )
                return result, worker_db is not None
            finally:
                _close_database(worker_db)

        result, isolated = await asyncio.to_thread(work)
        if not isolated and getattr(result, "source_backup", None) is not None:
            self.service.backup_service._insert(result.source_backup)
        return JobAccepted(job_id=uuid4().hex, state="running")

    clone = confirm_clone


class _UpdateFacade:
    def __init__(self, services: Mapping[str, UpdateService], profiles: Mapping[str, Any], adapters: Mapping[Any, Any]):
        self.services = services
        self.profiles = profiles
        self.adapters = adapters

    async def _stopped(self, profile: Any) -> None:
        adapter = self.adapters.get(profile.id) or self.adapters.get(_key(profile))
        if adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("profile_unavailable", "profile state could not be proven")
        value = adapter.observe(profile)
        observation = await value if inspect.isawaitable(value) else value
        if bool(getattr(observation, "running", False)):
            raise SafeError("profile_running", "profile is running; it must be stopped before update")

    def _stopped_sync(self, profile: Any) -> bool:
        adapter = self.adapters.get(profile.id) or self.adapters.get(_key(profile))
        if adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("profile_unavailable", "profile state could not be proven")
        value = adapter.observe(profile)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        if bool(getattr(value, "running", False)):
            raise SafeError("profile_running", "profile is running; it must be stopped before update")
        return True

    async def check(self, action: Any, actor: str | None = None, request_id: Any = None) -> UpdateStatus:
        key = _key(action.profile_id)
        try:
            result = await asyncio.to_thread(self.services[key].check, action.profile_id)
        except KeyError as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc
        return result

    async def confirm(
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None,
        lease_check: Callable[[], bool] | None = None,
    ) -> JobAccepted:
        key = _key((payload or {}).get("profile_id", action.profile_id))
        service = self.services[key]
        source_backup = service.backup_service
        await self._stopped(self.profiles[key])
        def work():
            worker_db = _isolated_database(service.database)
            worker_backup = BackupService(
                self.profiles[key],
                database=worker_db,
                stopped_check=lambda: self._stopped_sync(self.profiles[key]),
                free_space=source_backup.free_space,
                clock=source_backup.clock,
                tar_runner=source_backup.tar_runner,
            )
            worker_service = UpdateService(
                {key: self.profiles[key]},
                database=worker_db,
                backup_service=worker_backup,
                runner=service.runner,
                downloader=service.downloader,
                stage_release=service.stage_release,
                verify_release=service.verify_release,
                stopped_check=lambda *_: self._stopped_sync(self.profiles[key]),
                http_client=service.http_client,
                clock=service.clock,
                lease_check=lease_check,
            )
            try:
                return worker_service.apply(self.profiles[key]), worker_db is not None
            finally:
                _close_database(worker_db)

        try:
            result, isolated = await asyncio.to_thread(work)
        except KeyError as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc
        if not isolated:
            service._record(
                self.profiles[key],
                getattr(result, "state", "succeeded"),
                getattr(result, "prior_version", None),
                getattr(result, "new_version", None),
            )
        return JobAccepted(job_id=uuid4().hex, state="running")

    apply = confirm


class _NotificationFacade:
    def __init__(self, service: NotificationService):
        self.service = service

    async def get_config(self, action: Any, actor: str | None = None, request_id: Any = None) -> NotificationConfig:
        return self.service.get_config(action.profile_id)

    async def set_rule(self, action: Any, actor: str | None = None, request_id: Any = None) -> NotificationConfig:
        return self.service.set_rule(action, actor, request_id)

    async def test(self, action: Any, actor: str | None = None, request_id: Any = None) -> JobAccepted:
        def deliver() -> None:
            profile = self.service._get_profile(action.profile_id)
            secret = self.service._secret(action.channel)
            self.service._post(action.channel, secret, "game-control notification test")

        try:
            await asyncio.to_thread(deliver)
        except SafeError as exc:
            self.service._audit(actor or "system", "test_notification", _key(action.profile_id), "failed", exc.code)
            raise
        self.service._audit(actor or "system", "test_notification", _key(action.profile_id), "succeeded")
        return JobAccepted(job_id=uuid4().hex, state="running")

    async def send(self, profile_id: Any, event: Any, state_generation: int, message: str) -> bool:
        return await self.service.send_async(profile_id, event, state_generation, message)


class _AuditFacade:
    def __init__(self, database: Any):
        self.database = database

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
        """Decode the opaque keyset cursor; tolerate only legacy first-page 0."""
        if not cursor or cursor == "0":
            return None
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4))
            value = json.loads(raw)
            if (not isinstance(value, list) or len(value) != 2 or
                    not all(isinstance(item, str) and item for item in value)):
                raise ValueError
            return value[0], value[1]
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise SafeError("invalid_cursor", "invalid history cursor") from exc

    @staticmethod
    def _encode_cursor(timestamp: str, row_id: str) -> str:
        raw = json.dumps([timestamp, row_id], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    async def _query(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        """Run a history query without ever borrowing the controller writer DB."""
        # Unit/in-memory seams intentionally remain synchronous: sqlite's
        # default connection is thread-affine, and these injected databases do
        # not represent the production writer.  Real StateDatabase wrappers
        # always expose ``path`` and use the read-only branch below.
        if isinstance(self.database, sqlite3.Connection):
            try:
                return self.database.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                raise SafeError("state_unavailable", "operational state is unavailable") from exc

        loop = asyncio.get_running_loop()

        def run() -> list[tuple[Any, ...]]:
            connection = None
            try:
                path = getattr(self.database, "path", None)
                if path is None or Path(path).absolute() != Path(_AUDIT_STATE_DB_PATH).absolute():
                    raise SafeError("state_unavailable", "operational state is unavailable")
                connection = sqlite3.connect(
                    f"file:{Path(_AUDIT_STATE_DB_PATH)}?mode=ro", uri=True,
                    timeout=0.2,
                )
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA busy_timeout=200")
                return connection.execute(sql, params).fetchall()
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                raise SafeError("state_unavailable", "operational state is unavailable") from exc
            finally:
                if connection is not None:
                    connection.close()

        return await loop.run_in_executor(_AUDIT_QUERY_EXECUTOR, run)

    @classmethod
    def _page_cursor(cls, rows: list[tuple[Any, ...]], limit: int) -> str | None:
        return cls._encode_cursor(str(rows[-1][1]), str(rows[-1][0])) if len(rows) == limit else None

    async def list_events(self, action: Any, actor: str | None = None, request_id: Any = None) -> EventPage:
        limit = int(action.page.limit)
        cursor = self._decode_cursor(action.page.cursor)
        predicate = "" if cursor is None else "WHERE (timestamp,id) < (?,?) "
        params: tuple[Any, ...] = () if cursor is None else (cursor[0], cursor[1])
        rows = await self._query(
            f"SELECT id,timestamp,profile_id,code,message FROM events {predicate}"
            "ORDER BY timestamp DESC,id DESC LIMIT ?", params + (limit,)
        )
        items = tuple(
            EventSummary(
                id=str(row[0]),
                timestamp=_timestamp(row[1]),
                profile_id=ProfileId(row[2]) if row[2] else None,
                code=str(row[3])[:64],
                message=str(row[4])[:512],
            )
            for row in rows
        )
        return EventPage(items=items, next_cursor=self._page_cursor(rows, limit))

    async def list_audit(self, action: Any, actor: str | None = None, request_id: Any = None) -> AuditPage:
        limit = int(action.page.limit)
        cursor = self._decode_cursor(action.page.cursor)
        predicate = "" if cursor is None else "WHERE (timestamp,id) < (?,?) "
        params = () if cursor is None else (cursor[0], cursor[1])
        rows = await self._query(
            f"SELECT id,timestamp,actor,action,profile_id,result,error_code,detail FROM audit {predicate}"
            "ORDER BY timestamp DESC,id DESC LIMIT ?", params + (limit,)
        )
        items = tuple(
            AuditSummary(
                id=str(row[0]),
                timestamp=_timestamp(row[1]),
                actor=str(row[2])[:128],
                action=str(row[3])[:128],
                profile_id=ProfileId(row[4]) if row[4] else None,
                result=row[5],
                error_code=_error_code(row[6]),
                detail=str(row[7])[:512],
            )
            for row in rows
        )
        return AuditPage(items=items, next_cursor=self._page_cursor(rows, limit))


class _ProfilesFacade:
    def __init__(self, profiles: Mapping[str, Any]):
        self.profiles = profiles

    def public_profiles(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return tuple(
            PublicProfile(
                id=profile.id,
                display_name=profile.display_name,
                adapter=profile.adapter,
                operations=profile.operations,
                idle_stop_minutes=getattr(profile, "idle_stop_minutes", 0) or 0,
                public_endpoint=(
                    PublicEndpoint(
                        host=profile.public_endpoint.host,
                        port=profile.public_endpoint.port,
                        protocol=profile.public_endpoint.protocol,
                        reachable=None,
                    ) if profile.public_endpoint is not None else None
                ),
            )
            for profile in self.profiles.values()
        )


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _error_code(value: Any) -> ErrorCode | None:
    try:
        return ErrorCode(value) if value else None
    except ValueError:
        return None


class _StatsFacade:
    """Read-only bridge across lifecycle and disposable telemetry databases."""

    def __init__(self, state_database: Any, telemetry_database: Any) -> None:
        self.state_database = state_database
        self.telemetry_database = telemetry_database

    def tps(self, action: Any, *, now: str) -> dict[str, Any]:
        state = _connection(self.state_database)
        if state is None:
            raise RuntimeError("stats database unavailable")
        telemetry = _connection(self.telemetry_database)
        return query_stats_tps(
            state, action.profile_id.value, action.window, now=now, telemetry=telemetry,
            resolution=action.resolution, limit=action.limit,
        )


class ServiceSeams:
    """Concrete, non-empty service groups injected into ``Controller``."""

    def __init__(
        self,
        *,
        status: Any,
        logs: Any,
        backups: Any,
        worlds: Any,
        updates: Any,
        notifications: Any,
        audit: Any,
        profiles: Any,
        benchmarks: Any,
        session_store: Any | None = None,
        tps_sampler: Any | None = None,
        telemetry_db: Any | None = None,
        telemetry_sampler: Any | None = None,
        telemetry_db_owned: bool = False,
        telemetry_collectors: Any | None = None,
        stats: Any | None = None,
        alerts: Any | None = None,
    ):
        self.status = status
        self.logs = logs
        self.backups = backups
        self.worlds = worlds
        self.updates = updates
        self.notifications = notifications
        self.audit = audit
        self.profiles = profiles
        self.benchmarks = benchmarks
        self.session_store = session_store
        self.tps_sampler = tps_sampler
        self.telemetry_db = telemetry_db
        self.telemetry_sampler = telemetry_sampler
        self._telemetry_db_owned = bool(telemetry_db_owned)
        self._closed = False
        self.telemetry_collectors = telemetry_collectors
        self.stats = stats
        self.alerts = alerts

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._telemetry_db_owned and self.telemetry_db is not None and hasattr(self.telemetry_db, "close"):
            self.telemetry_db.close()

    async def aclose(self) -> Any:
        """Close telemetry off the event loop and return its close result."""
        if self._closed:
            return None
        self._closed = True
        if self.telemetry_collectors is not None and hasattr(self.telemetry_collectors, "close"):
            await self.telemetry_collectors.close()
        if not self._telemetry_db_owned or self.telemetry_db is None or not hasattr(self.telemetry_db, "close"):
            return None
        return await asyncio.to_thread(self.telemetry_db.close)


def build_service_seams(
    profiles: Any,
    adapters: Mapping[Any, Any],
    state_db: Any,
    slot_inspector: Any,
    *,
    secret_dir: str | Path = DEFAULT_SECRET_DIR,
    secret_values: tuple[str, ...] = (),
    stats_config: Mapping[str, Any] | None = None,
    b2_transport: Any | None = None,
    sunlit_online_backup: Any | None = None,
    benchmark_config: Any = None,
    telemetry_db: Any | None = None,
    rcon_telemetry: PersistentRconTelemetry | None = None,
    reservation_store: Any | None = None,
) -> ServiceSeams:
    opened_here = telemetry_db is None
    telemetry_db_owned = False
    if opened_here:
        state_path = getattr(state_db, "path", None)
        if state_path is not None:
            try:
                telemetry_db = TelemetryDatabase.open(Path(state_path).with_name("telemetry.db"))
                telemetry_db_owned = True
            except Exception as exc:
                raise RuntimeError("telemetry database failed closed") from exc
    try:
        return _build_service_seams_impl(
            profiles, adapters, state_db, slot_inspector,
            secret_dir=secret_dir, secret_values=secret_values, stats_config=stats_config,
            b2_transport=b2_transport, sunlit_online_backup=sunlit_online_backup,
            benchmark_config=benchmark_config, telemetry_db=telemetry_db,
            telemetry_db_owned=telemetry_db_owned,
            rcon_telemetry=rcon_telemetry, reservation_store=reservation_store,
        )
    except BaseException:
        if opened_here and telemetry_db is not None and hasattr(telemetry_db, "close"):
            telemetry_db.close()
        raise


def _build_service_seams_impl(
    profiles: Any,
    adapters: Mapping[Any, Any],
    state_db: Any,
    slot_inspector: Any,
    *,
    secret_dir: str | Path = DEFAULT_SECRET_DIR,
    secret_values: tuple[str, ...] = (),
    stats_config: Mapping[str, Any] | None = None,
    b2_transport: Any | None = None,
    sunlit_online_backup: Any | None = None,
    benchmark_config: Any = None,
    telemetry_db: Any | None = None,
    telemetry_db_owned: bool = False,
    rcon_telemetry: PersistentRconTelemetry | None = None,
    reservation_store: Any | None = None,
) -> ServiceSeams:
    profile_items = tuple(profiles)
    profile_map = {_key(profile): profile for profile in profile_items}
    adapter_map = {_key(profile): adapters.get(profile.id, adapters.get(_key(profile))) for profile in profile_items}
    sampler = MetricSampler()
    player_tracker = PlayerTracker()
    connection = _connection(state_db)
    session_store = SessionStore(connection) if connection is not None else None
    root_jobs = RootActiveJobsReader(state_db)
    root_generation = RootGenerationReader(state_db)
    root_wake = RootWakeSafetyEvidence(root_jobs, reservation_store)
    stats = stats_config if isinstance(stats_config, Mapping) else {}
    tick_profile_id = next(
        (
            _key(profile)
            for profile in profile_items
            if _key(profile) == ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
        ),
        "minecraft",
    )
    tps_sampler = (
        TpsSampler(
            connection,
            url=FIXED_EXPORTER_URL,
            profile_id=tick_profile_id,
            interval_seconds=stats.get("tps_interval_seconds", 30),
        )
        if connection is not None
        else None
    )
    if stats.get("exporter_url") is not None:
        # Phase 1 collector owns tick persistence; do not run the legacy
        # TpsSampler in parallel or emit duplicate metric_samples rows.
        tps_sampler = None
    health = {}
    for profile in profile_items:
        adapter = adapter_map[_key(profile)]

        def process_checker(
            current_profile: Any,
            observation: Any,
            *,
            connections: Mapping[str, list[Any]] | None = None,
            process_metrics: Any | None = None,
            _sampler=sampler,
        ) -> bool:
            pid = getattr(observation, "pid", None)
            if not isinstance(pid, int) or pid <= 0:
                pid = None
            sampled = process_metrics
            if sampled is None:
                sampled = _sampler.sample(
                    current_profile,
                    pid=pid,
                    track_rates=False,
                    connections=connections,
                )
            sampled = sampled.pid
            return sampled == pid if pid is not None else sampled is not None

        health[profile.id] = HealthChecker(adapter, process_checker=process_checker)
    status_service = StatusService(
        profile_items,
        adapters=adapter_map,
        slot_observer=slot_inspector.observe,
        active_jobs=root_jobs,
        health_checker=health,
        metrics=sampler,
        player_tracker=player_tracker,
        session_store=session_store,
        generation=root_generation,
        telemetry_db=telemetry_db,
        capability_evidence=root_wake,
        ups_health=_prometheus_ups_provider(stats.get("benchmark_ups")),
        benchmark_safety=BenchmarkPreflight(
            storage_paths=("/srv/game-servers", "/var/lib/game-control"),
            ups_health=_prometheus_ups_provider(stats.get("benchmark_ups")),
            session_store=session_store,
            wake_evidence=root_wake,
        ),
        storage_paths=("/srv/game-servers", "/var/lib/game-control"),
    )
    collector = _BoundTelemetryCollectors(
        profiles=profile_items,
        database=telemetry_db,
        stats=stats,
        rcon=None if rcon_telemetry is None else ResourceRef.owned(rcon_telemetry),
        player_tracker=player_tracker,
    )
    status_service.telemetry_collectors = collector

    async def sample_telemetry() -> None:
        snapshot = await status_service.snapshot(persist=True)
        running = {_key(item.profile_id): item for item in snapshot.profiles}
        await collector.collect(running=running)

    telemetry_sampler = TelemetrySampler(sample_telemetry, interval_seconds=5.0)
    status_service.telemetry_sampler = telemetry_sampler
    registry = SecretRegistry()
    for value in secret_values:
        registry.add(value)
    redactor = Redactor(registry)
    logs = _LogsFacade(profile_map, adapter_map, redactor)
    backups = _BackupFacade(
        profile_map,
        adapter_map,
        state_db,
        b2_transport=b2_transport,
        sunlit_online_backup=sunlit_online_backup,
        telemetry_db=telemetry_db,
    )
    backup_services = backups.services
    updates = {
        key: UpdateService({key: profile}, database=state_db, backup_service=backup_services[key])
        for key, profile in profile_map.items()
    }
    notification = NotificationService(
        profile_map,
        secret_dir=secret_dir,
        database=state_db,
        redactor=redactor,
    )
    alerts = _PerformanceAlerts(profile_map, notification)
    collector.alerts = alerts
    worlds = WorldService(
        profile_map.get(ProfileId.TERRARIA_VANILLA.value),
        profile_map.get(ProfileId.TERRARIA_TMOD.value),
        backup_service=backup_services.get(ProfileId.TERRARIA_VANILLA.value),
        stopped_check=lambda *_: True,
    )
    benchmark_service = BenchmarkService(
        parse_benchmark_plans(benchmark_config),
        database=state_db,
        adapters=adapter_map,
        profiles=profile_map,
        slot_inspector=slot_inspector,
    )
    return ServiceSeams(
        status=_StatusFacade(status_service),
        logs=logs,
        backups=backups,
        worlds=_WorldFacade(worlds, profile_map, adapter_map),
        updates=_UpdateFacade(updates, profile_map, adapter_map),
        notifications=_NotificationFacade(notification),
        audit=_AuditFacade(state_db),
        profiles=_ProfilesFacade(profile_map),
        benchmarks=benchmark_service,
        session_store=session_store,
        tps_sampler=tps_sampler,
        telemetry_db=telemetry_db,
        telemetry_sampler=telemetry_sampler,
        telemetry_db_owned=telemetry_db_owned,
        telemetry_collectors=collector,
        stats=_StatsFacade(state_db, telemetry_db),
        alerts=alerts,
    )


__all__ = ["ServiceSeams", "build_service_seams"]
