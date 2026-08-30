"""Composition-root adapters for the Horizon controller service groups.

Policy lives in the owning domain modules.  This module only normalizes the
fixed root configuration, constructs dependencies, and preserves the legacy
service-group names used by older callers.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .backups import BackupRpcFacade, BackupService, RestoreService
from .benchmark_safety import BenchmarkPreflight, prometheus_ups_provider
from .benchmarks import BenchmarkService, parse_benchmark_plans
from .capability_evidence import RootWakeSafetyEvidence
from .errors import SafeError
from .health import HealthChecker
from .history_queries import HistoryQueryService
from .logs import LogService
from .metrics import MetricSampler
from .players import PlayerTracker
from .session_store import SessionStore
from .models import ProfileId
from .notifications import DEFAULT_SECRET_DIR, NotificationRpcFacade, NotificationService
from .protocol import (
    AuditPage, EventPage, LogPage, PublicEndpoint, PublicProfile,
)
from .redaction import Redactor, SecretRegistry
from .root_state import RootActiveJobsReader, RootGenerationReader
from .runtime.alerts import AlertRuntime
from .runtime.protocols import AlertObservation
from .runtime.telemetry import (
    DEFAULT_HOST_METRICS, LegacyTpsMode, ResourceRef, TelemetryCollector,
    TelemetryRuntime, TelemetryRuntimeConfig,
)
from .rcon_telemetry import PerformanceResult as _PerformanceResult
from .rcon_telemetry import PlayerCountResult as _PlayerCountResult
from .rcon_telemetry import TelemetryCommand as _TelemetryCommand
PerformanceResult = _PerformanceResult
PlayerCountResult = _PlayerCountResult
TelemetryCommand = _TelemetryCommand
from .state_db import STATE_DB_PATH
from .status import StatusService
from .tps import FIXED_EXPORTER_URL, TpsSampler
from .telemetry_db import TelemetryDatabase
from .updates import UpdateRpcFacade, UpdateService
from .worlds import WorldRpcFacade, WorldService

SUNLIT_ONLINE_SNAPSHOT_SECONDS = 240.0
_HOST_TELEMETRY_METRICS = DEFAULT_HOST_METRICS
_AUDIT_STATE_DB_PATH = STATE_DB_PATH
_prometheus_ups_provider = prometheus_ups_provider


def _key(value: Any) -> str:
    value = getattr(value, "id", value)
    value = getattr(value, "value", value)
    return str(value)


def _connection(database: Any) -> Any | None:
    value = getattr(database, "connection", database)
    return value if hasattr(value, "execute") else None


def _isolated_database(database: Any) -> Any | None:
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


class _StatusFacade:
    def __init__(self, service: StatusService):
        self.service = service

    async def snapshot(self, action: Any = None, actor: str | None = None, request_id: Any = None, *, maintenance: bool = False):
        return await self.service.snapshot(persist=False, force=bool(maintenance or getattr(action, "refresh", False)))

    async def cached_snapshot(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return await self.service.cached_snapshot()

    def telemetry_health(self) -> dict[str, Any]:
        return self.service.telemetry_health()

    async def benchmark_eligibility(self, *, maintenance_window: bool, rollback_safe: bool, public_wake_policy: str, snapshot: Any = None):
        return await self.service.benchmark_eligibility(maintenance_window=maintenance_window, rollback_safe=rollback_safe, public_wake_policy=public_wake_policy, snapshot=snapshot)


def _legacy_telemetry_config(stats: Mapping[str, Any], approved_profile_ids: tuple[str, ...]):
    """Translate the pre-runtime root section at this one compatibility edge."""
    if not isinstance(stats, Mapping):
        raise ValueError("legacy telemetry settings are invalid")
    allowed = {"exporter_url", "tick_profile", "log_checkpoint_dir", "gc_log_path", "gc_profile_id", "host_metrics", "legacy_tps_mode", "legacy_tps_interval_seconds", "tps_interval_seconds"}
    if set(stats) - allowed:
        raise ValueError("legacy telemetry settings contain unknown keys")
    approved = tuple(str(getattr(item, "value", item)) for item in approved_profile_ids)
    settings: dict[str, Any] = {"legacy_tps_mode": stats.get("legacy_tps_mode", "disabled")}
    for key in ("log_checkpoint_dir", "host_metrics", "legacy_tps_interval_seconds"):
        if key in stats:
            settings[key] = stats[key]
    if "legacy_tps_interval_seconds" not in settings and "tps_interval_seconds" in stats:
        settings["legacy_tps_interval_seconds"] = stats["tps_interval_seconds"]
    if stats.get("exporter_url") is not None and str(stats.get("legacy_tps_mode", "disabled")) != LegacyTpsMode.ENABLED.value:
        tick_profile = stats.get("tick_profile")
        if tick_profile is None and ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value in approved:
            tick_profile = ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
        tick_profile = None if tick_profile is None else str(getattr(tick_profile, "value", tick_profile))
        if tick_profile is not None and tick_profile not in approved:
            raise ValueError("legacy tick profile is not in the approved root registry")
        if tick_profile is not None:
            settings.update(exporter_url=stats["exporter_url"], tick_profile=tick_profile)
    gc_profile, gc_path = stats.get("gc_profile_id"), stats.get("gc_log_path")
    if gc_profile is not None or gc_path is not None:
        settings.update(gc_profile_id=str(getattr(gc_profile, "value", gc_profile)), gc_log_path=gc_path)
    return TelemetryRuntimeConfig.from_root_config(settings, approved_profile_ids=approved)


class _LegacyAlertSink:
    def __init__(self, target: Any):
        self.target = target

    def observe(self, observation: Any) -> None:
        if isinstance(observation, AlertObservation):
            self.target.observe(observation)


class _BoundTelemetryCollectors(TelemetryCollector):
    """Bounded compatibility call-shape adapter for pre-composition tests."""
    def __init__(self, *, profiles: tuple[Any, ...], database: Any, stats: Mapping[str, Any], rcon: Any, player_tracker: Any, alerts: Any | None = None):
        self._legacy_database = database.value if isinstance(database, ResourceRef) else database
        self._legacy_rcon = rcon.value if isinstance(rcon, ResourceRef) else rcon
        self._legacy_rcon_ref = rcon if isinstance(rcon, ResourceRef) else (None if rcon is None else ResourceRef.borrowed(rcon))
        self._legacy_alerts = alerts
        super().__init__(profiles=profiles, config=_legacy_telemetry_config(stats, tuple(_key(item) for item in profiles)), database=database, rcon=rcon, player_tracker=player_tracker, alert_sink=None if alerts is None else _LegacyAlertSink(alerts))

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
            value = running.get(_key(profile), False)
            statuses.append(value if not isinstance(value, bool) else type("LegacyStatus", (), {"profile_id": _key(profile), "state": "running" if value else "stopped", "pid": None, "rss_bytes": None})())
        await super().collect(type("LegacySnapshot", (), {"profiles": tuple(statuses)})())

    async def close(self) -> None:
        await self.aclose()
        drain = getattr(self._legacy_database, "drain", None)
        cleanup_error: BaseException | None = None
        cancelled = False
        if callable(drain):
            operation = asyncio.ensure_future(asyncio.to_thread(drain, 10.0))
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
            try:
                if operation.result() is not True:
                    cleanup_error = RuntimeError("legacy telemetry database drain failed")
            except BaseException as exc:
                cleanup_error = exc
        elif self._legacy_database is not None:
            cleanup_error = RuntimeError("legacy telemetry database drain is unavailable")
        if self._legacy_rcon_ref is not None and self._legacy_rcon_ref.owns_value:
            close = getattr(self._legacy_rcon, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            elif cleanup_error is None:
                cleanup_error = RuntimeError("legacy telemetry RCON close is unavailable")
        if cancelled:
            raise asyncio.CancelledError
        if cleanup_error is not None:
            raise cleanup_error


class _PerformanceAlerts:
    """Compatibility translator into the one AlertRuntime owner."""
    def __init__(self, profiles: Mapping[str, Any], notifications: Any, *, max_pending: int = 8):
        self._runtime = AlertRuntime(profiles, notifications, max_pending=max_pending)

    def observe(self, profile_id: Any, **values: Any) -> None:
        self._runtime.observe(AlertObservation(profile_id=_key(profile_id), profile_state=values.get("profile_state", "running"), now=values.get("now", 0), mspt_p95=values.get("mspt_p95"), rss_bytes=values.get("rss_bytes"), wake_duration_ms=values.get("wake_duration_ms"), benchmark_regression=values.get("benchmark_regression")))

    async def close(self) -> None:
        await self._runtime.close()


class _LogsFacade:
    def __init__(self, profiles: Mapping[str, Any], adapters: Mapping[Any, Any], redactor: Redactor):
        self.profiles, self.adapters, self.redactor = profiles, adapters, redactor
        self._services = {key: LogService(adapters.get(key) or adapters.get(getattr(profile, "id", None)), redactor=redactor) for key, profile in profiles.items()}

    async def page(self, action: Any, actor: str | None = None, request_id: Any = None) -> LogPage:
        key = _key(action.profile_id)
        if key not in self.profiles:
            raise SafeError("profile_not_found", "profile was not found")
        options = action.page
        lines = await self._services[key].tail(self.profiles[key], limit=options.limit, since=options.since, until=options.until)
        severity = getattr(options, "severity", "all")
        if severity != "all":
            lines = tuple(line for line in lines if line.severity == severity)
        return LogPage(items=tuple(lines), next_cursor=None)

    async def tail(self, profile: Any, *, limit: int = 500, since: datetime | None = None):
        return await self._services[_key(profile)].tail(self.profiles[_key(profile)], limit=limit, since=since)

    async def search(self, profile: Any, query: str, *, limit: int = 100, regex: bool = False):
        return await self._services[_key(profile)].search(self.profiles[_key(profile)], query, limit=limit, regex=regex)


class _AuditFacade:
    """Import-compatible, SQL-free adapter over the history owner."""
    _decode_cursor = staticmethod(HistoryQueryService._decode_cursor)
    _encode_cursor = staticmethod(HistoryQueryService._encode_cursor)
    _page_cursor = classmethod(HistoryQueryService._page_cursor.__func__)

    def __init__(self, database: Any, history: HistoryQueryService | None = None):
        self.database = database
        self.history = history or HistoryQueryService(database, approved_state_path=_AUDIT_STATE_DB_PATH)

    async def list_events(self, action: Any, actor: str | None = None, request_id: Any = None) -> EventPage:
        return await self.history.list_events(action, actor, request_id)

    async def list_audit(self, action: Any, actor: str | None = None, request_id: Any = None) -> AuditPage:
        return await self.history.list_audit(action, actor, request_id)


class _ProfilesFacade:
    def __init__(self, profiles: Mapping[str, Any]):
        self.profiles = profiles

    def public_profiles(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return tuple(PublicProfile(id=profile.id, display_name=profile.display_name, adapter=profile.adapter, operations=profile.operations, idle_stop_minutes=getattr(profile, "idle_stop_minutes", 0) or 0, public_endpoint=(PublicEndpoint(host=profile.public_endpoint.host, port=profile.public_endpoint.port, protocol=profile.public_endpoint.protocol, reachable=None) if profile.public_endpoint is not None else None)) for profile in self.profiles.values())


class _StatsFacade:
    """Import-compatible adapter; all production queries belong to history."""
    def __init__(self, state_database: Any, telemetry_database: Any | None = None, history: HistoryQueryService | None = None):
        self.state_database = state_database
        self.telemetry_database = telemetry_database
        self.history = history or HistoryQueryService(state_database, telemetry_database, approved_state_path=_AUDIT_STATE_DB_PATH)

    def tps(self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.history.tps_sync(action, now=now)
        return self.history.tps(action, actor, request_id, now=now)

    async def stats_summary(self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str) -> dict[str, Any]:
        return await self.history.stats_summary(action, actor, request_id, now=now)

    async def stats_heatmap(self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str) -> dict[str, Any]:
        return await self.history.stats_heatmap(action, actor, request_id, now=now)


class _ContainerSlot:
    def __init__(self):
        self._container: Any | None = None

    @property
    def container(self) -> Any | None:
        return self._container

    def bind(self, container: Any) -> None:
        if self._container is not None:
            raise RuntimeError("service container is already bound")
        self._container = container


class ServiceSeams:
    """Complete public service-group surface plus additive runtime ownership."""
    def __init__(self, *, status: Any, logs: Any, backups: Any, worlds: Any, updates: Any, notifications: Any, audit: Any, profiles: Any, benchmarks: Any, session_store: Any | None = None, tps_sampler: Any | None = None, telemetry_db: Any | None = None, telemetry_sampler: Any | None = None, telemetry_db_owned: bool = False, telemetry_collectors: Any | None = None, stats: Any | None = None, alerts: Any | None = None, telemetry_runtime: Any | None = None, container: Any | None = None, container_slot: _ContainerSlot | None = None, crafty_adapters: tuple[Any, ...] = ()):
        self.status, self.logs, self.backups, self.worlds, self.updates = status, logs, backups, worlds, updates
        self.notifications, self.audit, self.profiles, self.benchmarks = notifications, audit, profiles, benchmarks
        self.session_store, self.tps_sampler, self.telemetry_db, self.telemetry_sampler = session_store, tps_sampler, telemetry_db, telemetry_sampler
        self.telemetry_collectors, self.stats, self.alerts = telemetry_collectors, stats, alerts
        self.telemetry_runtime = telemetry_runtime
        self._crafty_adapters = tuple(crafty_adapters)
        self._container_slot = container_slot or _ContainerSlot()
        if container is not None:
            self._container_slot.bind(container)
        self._close_owner = container

    @property
    def container(self) -> Any | None:
        return self._container_slot.container

    def _finalize_container(self, container: Any) -> None:
        self._container_slot.bind(container)
        self._close_owner = container

    def close(self) -> None:
        if self._close_owner is None:
            raise RuntimeError("service container close owner is not finalized")
        self._close_owner.close()

    async def aclose(self) -> None:
        if self._close_owner is None:
            raise RuntimeError("service container close owner is not finalized")
        result = self._close_owner.aclose()
        if inspect.isawaitable(result):
            await result


def build_service_seams(profiles: Any, adapters: Mapping[Any, Any], state_db: Any, slot_inspector: Any, *, secret_dir: str | Path = DEFAULT_SECRET_DIR, secret_values: tuple[str, ...] = (), stats_config: Mapping[str, Any] | None = None, b2_transport: Any | None = None, sunlit_online_backup: Any | None = None, benchmark_config: Any = None, telemetry_db: Any | None = None, rcon_telemetry: Any | None = None, reservation_store: Any | None = None, telemetry_runtime: TelemetryRuntime | None = None, alert_runtime: AlertRuntime | None = None, history_queries: HistoryQueryService | None = None, container_slot: _ContainerSlot | None = None, crafty_adapters: tuple[Any, ...] = (), own_telemetry_resources: bool = False, register_owned: Callable[[Any, Any], Any] | None = None) -> ServiceSeams:
    profile_items = tuple(profiles)
    profile_map = {_key(profile): profile for profile in profile_items}
    adapter_map = {_key(profile): adapters.get(getattr(profile, "id", None), adapters.get(_key(profile))) for profile in profile_items}
    sampler = MetricSampler()
    player_tracker = PlayerTracker()
    connection = _connection(state_db)
    session_store = SessionStore(connection) if connection is not None else None
    root_jobs, root_generation = RootActiveJobsReader(state_db), RootGenerationReader(state_db)
    root_wake = RootWakeSafetyEvidence(root_jobs, reservation_store)
    stats = stats_config if isinstance(stats_config, Mapping) else {}
    config = _legacy_telemetry_config(stats, tuple(profile_map))
    if telemetry_db is None and getattr(state_db, "path", None) is not None:
        telemetry_db = TelemetryDatabase.open(Path(state_db.path).with_name("telemetry.db"))
        if register_owned is not None:
            register_owned(telemetry_db, telemetry_db.close)
    notification_service = NotificationService(profile_map, secret_dir=secret_dir, database=state_db, redactor=Redactor(SecretRegistry(secret_values)))
    if register_owned is not None:
        register_owned(notification_service, notification_service.close)
    alert_was_injected = alert_runtime is not None
    alert_runtime = alert_runtime or AlertRuntime(profile_map, notification_service)
    if register_owned is not None and not alert_was_injected:
        register_owned(alert_runtime, alert_runtime.close)
    collector = TelemetryCollector(profiles=profile_items, config=config, database=telemetry_db, rcon=rcon_telemetry, player_tracker=player_tracker, alert_sink=alert_runtime)
    def process_checker(current_profile: Any, observation: Any, *, connections: Mapping[str, list[Any]] | None = None, process_metrics: Any | None = None) -> bool:
        pid = getattr(observation, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            pid = None
        sampled = process_metrics
        if sampled is None:
            sampled = sampler.sample(current_profile, pid=pid, track_rates=False, connections=connections)
        sampled_pid = getattr(sampled, "pid", None)
        return sampled_pid == pid if pid is not None else sampled_pid is not None
    status_health = {profile.id: HealthChecker(adapter, process_checker=process_checker) for profile, adapter in ((profile, adapter_map[_key(profile)]) for profile in profile_items)}
    status_service = StatusService(profile_items, adapters=adapter_map, slot_observer=slot_inspector.observe, active_jobs=root_jobs, health_checker=status_health, metrics=sampler, player_tracker=player_tracker, session_store=session_store, generation=root_generation, telemetry_db=telemetry_db, capability_evidence=root_wake, ups_health=_prometheus_ups_provider(stats.get("benchmark_ups")), benchmark_safety=BenchmarkPreflight(storage_paths=("/srv/game-servers", "/var/lib/game-control"), ups_health=_prometheus_ups_provider(stats.get("benchmark_ups")), session_store=session_store, wake_evidence=root_wake), storage_paths=("/srv/game-servers", "/var/lib/game-control"))
    runtime_was_injected = telemetry_runtime is not None
    telemetry_runtime = telemetry_runtime or TelemetryRuntime(status_service, collector, database=(ResourceRef.owned(telemetry_db) if own_telemetry_resources else ResourceRef.borrowed(telemetry_db)) if telemetry_db is not None else None, rcon=(ResourceRef.owned(rcon_telemetry) if own_telemetry_resources else ResourceRef.borrowed(rcon_telemetry)) if rcon_telemetry is not None else None)
    if register_owned is not None and not runtime_was_injected:
        register_owned(telemetry_runtime, telemetry_runtime.close)
    status_service.telemetry_collectors, status_service.telemetry_sampler = collector, telemetry_runtime.sampler
    logs = _LogsFacade(profile_map, adapter_map, Redactor(SecretRegistry(secret_values)))
    backups = BackupRpcFacade(profile_map, adapter_map, state_db, b2_transport=b2_transport, sunlit_online_backup=sunlit_online_backup, telemetry_db=telemetry_db)
    update_services: dict[str, UpdateService] = {}
    for key, profile in profile_map.items():
        update = UpdateService({key: profile}, database=state_db, backup_service=backups.services[key])
        update_services[key] = update
        if register_owned is not None:
            register_owned(update, update.aclose)
    worlds = WorldService(profile_map.get(ProfileId.TERRARIA_VANILLA.value), profile_map.get(ProfileId.TERRARIA_TMOD.value), backup_service=backups.services.get(ProfileId.TERRARIA_VANILLA.value), stopped_check=lambda *_: True)
    benchmark_service = BenchmarkService(parse_benchmark_plans(benchmark_config), database=state_db, adapters=adapter_map, profiles=profile_map, slot_inspector=slot_inspector)
    history_was_injected = history_queries is not None
    history_queries = history_queries or HistoryQueryService(state_db, telemetry_db, approved_state_path=_AUDIT_STATE_DB_PATH, approved_telemetry_path=Path(_AUDIT_STATE_DB_PATH).with_name("telemetry.db"))
    if register_owned is not None and not history_was_injected:
        register_owned(history_queries, history_queries.aclose)
    legacy_sampler = None
    if config.legacy_tps_mode is LegacyTpsMode.ENABLED and connection is not None:
        legacy_profile = ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value if ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value in profile_map else next(iter(profile_map), "minecraft")
        legacy_sampler = TpsSampler(connection, url=FIXED_EXPORTER_URL, profile_id=legacy_profile, interval_seconds=config.legacy_tps_interval_seconds)
    return ServiceSeams(status=_StatusFacade(status_service), logs=logs, backups=backups, worlds=WorldRpcFacade(worlds, profile_map, adapter_map), updates=UpdateRpcFacade(update_services, profile_map, adapter_map), notifications=NotificationRpcFacade(notification_service), audit=_AuditFacade(state_db, history_queries), profiles=_ProfilesFacade(profile_map), benchmarks=benchmark_service, session_store=session_store, tps_sampler=legacy_sampler, telemetry_db=telemetry_db, telemetry_sampler=telemetry_runtime.sampler, telemetry_runtime=telemetry_runtime, telemetry_collectors=collector, stats=_StatsFacade(state_db, telemetry_db, history_queries), alerts=alert_runtime, container_slot=container_slot, crafty_adapters=crafty_adapters)


StatsCompatibilityFacade = _StatsFacade
class _BackupFacade(BackupRpcFacade):
    """Legacy test/import name with injectable module-level factories."""
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("backup_service_factory", BackupService)
        kwargs.setdefault("restore_service_factory", RestoreService)
        kwargs.setdefault("isolated_database_factory", _isolated_database)
        kwargs.setdefault("close_database", _close_database)
        super().__init__(*args, **kwargs)


class _WorldFacade(WorldRpcFacade):
    pass


class _UpdateFacade(UpdateRpcFacade):
    def __init__(self, *args: Any, **kwargs: Any):
        kwargs.setdefault("update_service_factory", UpdateService)
        super().__init__(*args, **kwargs)


_NotificationFacade = NotificationRpcFacade

# Historical tests and downstream source lanes imported the implementation
# seam directly; keep the name as a policy-free compatibility alias.
_build_service_seams_impl = build_service_seams

__all__ = ["HistoryQueryService", "PerformanceResult", "PlayerCountResult", "ResourceRef", "ServiceSeams", "StatsCompatibilityFacade", "TelemetryCommand", "TelemetryRuntime", "TelemetryRuntimeConfig", "_AuditFacade", "_BackupFacade", "_BoundTelemetryCollectors", "_ContainerSlot", "_LogsFacade", "_NotificationFacade", "_PerformanceAlerts", "_ProfilesFacade", "_StatsFacade", "_StatusFacade", "_UpdateFacade", "_WorldFacade", "build_service_seams"]
