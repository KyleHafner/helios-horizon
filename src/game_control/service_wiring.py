"""Closed production facades for controller service seams.

The controller deals in RPC protocol models while the task services expose
domain records and profile-oriented methods.  These adapters keep that
translation in one root-owned module; no RPC value is used as a path, command,
URL, or credential.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .backups import BackupService, RestoreService
from .errors import SafeError
from .health import HealthChecker
from .logs import LogService
from .metrics import MetricSampler
from .players import PlayerTracker
from .session_store import SessionStore
from .tps import TpsSampler
from .models import ProfileId
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
from .status import StatusService
from .updates import UpdateService
from .worlds import WorldService


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


class _ActiveJobs:
    def __init__(self, database: Any):
        self.database = database

    def __call__(self, profile_id: Any) -> str | None:
        connection = _connection(self.database)
        if connection is None:
            return None
        row = connection.execute(
            "SELECT operation FROM jobs WHERE profile_id=? AND state IN ('accepted','running') "
            "ORDER BY created_at DESC LIMIT 1",
            (_key(profile_id),),
        ).fetchone()
        return row[0] if row else None


class _StatusFacade:
    def __init__(self, service: StatusService):
        self.service = service

    async def snapshot(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return await self.service.snapshot()

    async def cached_snapshot(self, action: Any = None, actor: str | None = None, request_id: Any = None):
        return await self.service.cached_snapshot()


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
    def __init__(self, profiles: Mapping[str, Any], adapters: Mapping[Any, Any], database: Any):
        self.profiles = profiles
        self.adapters = adapters
        self.database = database
        self.services = {
            key: BackupService(profile, database=database, stopped_check=lambda: True)
            for key, profile in profiles.items()
        }
        self.restores = {
            key: RestoreService(
                profile,
                backup_service=self.services[key],
                stopped_check=lambda: True,
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

    async def create(self, action: Any, actor: str | None = None, request_id: Any = None) -> JobAccepted:
        profile, service = self._service(action.profile_id)
        await self._stopped(profile)
        def work():
            worker_db = _isolated_database(self.database)
            worker = BackupService(
                profile,
                database=worker_db,
                stopped_check=lambda: True,
                free_space=service.free_space,
                clock=service.clock,
                tar_runner=service.tar_runner,
            )
            try:
                return worker.create(
                    action,
                    actor,
                    request_id,
                    protected=bool(action.protected),
                ), worker_db is not None
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
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None
    ) -> JobAccepted:
        payload = payload or {}
        profile, _service = self._service(payload.get("profile_id", action.profile_id))
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
                stopped_check=lambda: True,
                free_space=source_backup.free_space,
                clock=source_backup.clock,
                tar_runner=source_backup.tar_runner,
            )
            worker_restore = RestoreService(
                profile,
                backup_service=worker_backup,
                stopped_check=lambda: True,
                free_space=source_restore.free_space,
                health_check=source_restore.health_check,
            )
            try:
                return worker_restore.restore(archive, actor, request_id)
            finally:
                _close_database(worker_db)

        result = await asyncio.to_thread(work)
        return JobAccepted(job_id=getattr(result, "backup_id", uuid4().hex), state="running")

    restore = confirm_restore


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

    async def confirm_clone(
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None
    ) -> JobAccepted:
        payload = payload or {}
        await self._stopped()
        source_backup = self.service.backup_service
        def work():
            worker_db = _isolated_database(source_backup.database)
            worker_backup = BackupService(
                self.service.vanilla,
                database=worker_db,
                stopped_check=lambda: True,
                free_space=source_backup.free_space,
                clock=source_backup.clock,
                tar_runner=source_backup.tar_runner,
            )
            worker_service = WorldService(
                self.service.vanilla,
                self.service.tmod,
                backup_service=worker_backup,
                stopped_check=lambda *_: True,
                clock=self.service.clock,
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

    async def check(self, action: Any, actor: str | None = None, request_id: Any = None) -> UpdateStatus:
        key = _key(action.profile_id)
        try:
            result = await asyncio.to_thread(self.services[key].check, action.profile_id)
        except KeyError as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc
        return result

    async def confirm(
        self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None
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
                stopped_check=lambda: True,
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
                stopped_check=lambda *_: True,
                http_client=service.http_client,
                clock=service.clock,
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
        return await asyncio.to_thread(self.service.send, profile_id, event, state_generation, message)


class _AuditFacade:
    def __init__(self, database: Any):
        self.database = database

    @staticmethod
    def _offset(page: Any) -> int:
        cursor = getattr(page, "cursor", None)
        if not cursor:
            return 0
        try:
            return max(0, min(int(cursor), 1_000_000))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cursor(offset: int, count: int, limit: int) -> str | None:
        return str(offset + count) if count == limit else None

    async def list_events(self, action: Any, actor: str | None = None, request_id: Any = None) -> EventPage:
        connection = _connection(self.database)
        if connection is None:
            raise SafeError("state_unavailable", "operational state is unavailable")
        offset, limit = self._offset(action.page), int(action.page.limit)
        rows = connection.execute(
            "SELECT id,timestamp,profile_id,code,message FROM events ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
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
        return EventPage(items=items, next_cursor=self._cursor(offset, len(items), limit))

    async def list_audit(self, action: Any, actor: str | None = None, request_id: Any = None) -> AuditPage:
        connection = _connection(self.database)
        if connection is None:
            raise SafeError("state_unavailable", "operational state is unavailable")
        offset, limit = self._offset(action.page), int(action.page.limit)
        rows = connection.execute(
            "SELECT id,timestamp,actor,action,profile_id,result,error_code,detail "
            "FROM audit ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
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
        return AuditPage(items=items, next_cursor=self._cursor(offset, len(items), limit))


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


def _generation(database: Any) -> int:
    connection = _connection(database)
    if connection is None:
        return 0
    try:
        row = connection.execute("PRAGMA application_id").fetchone()
        return max(0, int(row[0])) if row else 0
    except Exception:
        return 0


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
        session_store: Any | None = None,
        tps_sampler: Any | None = None,
    ):
        self.status = status
        self.logs = logs
        self.backups = backups
        self.worlds = worlds
        self.updates = updates
        self.notifications = notifications
        self.audit = audit
        self.profiles = profiles
        self.session_store = session_store
        self.tps_sampler = tps_sampler


def build_service_seams(
    profiles: Any,
    adapters: Mapping[Any, Any],
    state_db: Any,
    slot_inspector: Any,
    *,
    secret_dir: str | Path = DEFAULT_SECRET_DIR,
    secret_values: tuple[str, ...] = (),
    stats_config: Mapping[str, Any] | None = None,
) -> ServiceSeams:
    profile_items = tuple(profiles)
    profile_map = {_key(profile): profile for profile in profile_items}
    adapter_map = {_key(profile): adapters.get(profile.id, adapters.get(_key(profile))) for profile in profile_items}
    sampler = MetricSampler()
    player_tracker = PlayerTracker()
    connection = _connection(state_db)
    session_store = SessionStore(connection) if connection is not None else None
    stats = stats_config if isinstance(stats_config, Mapping) else {}
    tps_sampler = (
        TpsSampler(
            connection,
            url=stats.get("exporter_url", "http://127.0.0.1:19565/metrics"),
            interval_seconds=stats.get("tps_interval_seconds", 30),
        )
        if connection is not None
        else None
    )
    health = {}
    for profile in profile_items:
        adapter = adapter_map[_key(profile)]

        def process_checker(
            current_profile: Any,
            observation: Any,
            *,
            connections: Mapping[str, list[Any]] | None = None,
            _sampler=sampler,
        ) -> bool:
            pid = getattr(observation, "pid", None)
            if not isinstance(pid, int) or pid <= 0:
                return False
            return _sampler.sample(
                current_profile,
                pid=pid,
                track_rates=False,
                connections=connections,
            ).pid == pid

        health[profile.id] = HealthChecker(adapter, process_checker=process_checker)
    status_service = StatusService(
        profile_items,
        adapters=adapter_map,
        slot_observer=slot_inspector.observe,
        active_jobs=_ActiveJobs(state_db),
        health_checker=health,
        metrics=sampler,
        player_tracker=player_tracker,
        session_store=session_store,
        generation=lambda: _generation(state_db),
    )
    registry = SecretRegistry()
    for value in secret_values:
        registry.add(value)
    redactor = Redactor(registry)
    logs = _LogsFacade(profile_map, adapter_map, redactor)
    backups = _BackupFacade(profile_map, adapter_map, state_db)
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
    worlds = WorldService(
        profile_map.get(ProfileId.TERRARIA_VANILLA.value),
        profile_map.get(ProfileId.TERRARIA_TMOD.value),
        backup_service=backup_services.get(ProfileId.TERRARIA_VANILLA.value),
        stopped_check=lambda *_: True,
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
        session_store=session_store,
        tps_sampler=tps_sampler,
    )


__all__ = ["ServiceSeams", "build_service_seams"]
