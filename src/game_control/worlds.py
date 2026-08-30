"""Safe Vanilla-to-tModLoader world cloning."""

from __future__ import annotations

import hashlib
import asyncio
import inspect
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import SafeError
from .backups import BackupService
from .models import ProfileId
from .protocol import JobAccepted


def _rpc_key(value: Any) -> str:
    value = getattr(value, "id", value)
    value = getattr(value, "value", value)
    return str(value)


def _close_database(database: Any | None) -> None:
    if database is not None and hasattr(database, "close"):
        database.close()


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


@dataclass(frozen=True)
class WorldCloneResult:
    source: Path
    destination: Path
    source_backup: Any
    source_sha256: str
    destination_sha256: str


class WorldService:
    def __init__(
        self,
        vanilla_profile: Any,
        tmod_profile: Any,
        *,
        backup_service: Any,
        stopped_check: Callable[..., bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        lease_check: Callable[[], bool] | None = None,
    ) -> None:
        self.vanilla = vanilla_profile
        self.tmod = tmod_profile
        self.backup_service = backup_service
        self.stopped_check = stopped_check
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lease_check = lease_check

    def _assert_lease(self) -> None:
        if self.lease_check is not None and not self.lease_check():
            raise SafeError("slot_conflict", "operation lease was lost before publication")

    def clone_vanilla_to_tmod(
        self,
        source_world_id: str,
        destination_name: str,
        actor: str | None = None,
        request_id: Any | None = None,
        **_: Any,
    ) -> WorldCloneResult:
        source, base_name = self._source(source_world_id)
        if not _NAME.fullmatch(destination_name):
            raise SafeError("invalid_world", "world name is not approved")
        self._stopped()
        if not source.is_file() or source.is_symlink():
            raise SafeError("world_not_found", "source world was not found")
        source_hash = _sha256(source)
        backup = self.backup_service.create(protected=True)
        destination_root = Path(self.tmod.paths.mutable_root)
        destination_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        destination = destination_root / f"{destination_name}-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}{source.suffix}"
        staging = destination_root / f".clone-{uuid.uuid4().hex}"
        if destination.exists():
            raise SafeError("destination_exists", "world destination already exists")
        try:
            staging.mkdir(mode=0o700)
            target = staging / f"{base_name}{source.suffix}"
            _copy_readonly(source, target)
            destination_hash = _sha256(target)
            if destination_hash != source_hash or _sha256(source) != source_hash:
                raise SafeError("clone_failed", "source and destination checksums differ")
            os.chmod(target, 0o640)
            # Hard-linking is an exclusive publish on the same filesystem;
            # unlike replace(), it cannot overwrite a concurrent destination.
            self._assert_lease()
            os.link(target, destination)
            target.unlink()
            os.rmdir(staging)
            return WorldCloneResult(source, destination, backup, source_hash, destination_hash)
        except SafeError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SafeError("clone_failed", "world clone could not be completed") from exc

    def clone_to_exact_existing_destination(self, source_world_id: str, destination: str | os.PathLike[str]) -> WorldCloneResult:
        destination_path = Path(destination)
        if destination_path.exists():
            raise SafeError("destination_exists", "world destination already exists")
        return self.clone_vanilla_to_tmod(source_world_id, destination_path.name)

    def _source(self, world_id: str) -> tuple[Path, str]:
        if not isinstance(world_id, str) or not world_id or "/" in world_id or "\\" in world_id or world_id in {".", ".."}:
            raise SafeError("invalid_world", "source world is not approved")
        source_root = Path(self.vanilla.paths.mutable_root)
        source = source_root / world_id
        return source, Path(world_id).stem

    def _stopped(self) -> None:
        if self.stopped_check is None:
            raise SafeError("profile_running", "profiles must be stopped before clone")
        try:
            stopped = self.stopped_check(self.vanilla, self.tmod)
        except TypeError:
            stopped = self.stopped_check()
        if not stopped:
            raise SafeError("profile_running", "profiles must be stopped before clone")


def _copy_readonly(source: Path, target: Path) -> None:
    with source.open("rb") as source_stream, target.open("xb") as target_stream:
        while True:
            chunk = source_stream.read(1024 * 1024)
            if not chunk:
                break
            target_stream.write(chunk)
        target_stream.flush()
        os.fsync(target_stream.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WorldRpcFacade:
    """Typed RPC translator for paired, stopped world cloning."""

    def __init__(self, service: WorldService, profiles: Mapping[str, Any], adapters: Mapping[Any, Any]):
        self.service, self.profiles, self.adapters = service, profiles, adapters

    async def _stopped(self) -> None:
        for profile_id in (ProfileId.TERRARIA_VANILLA.value, ProfileId.TERRARIA_TMOD.value):
            profile = self.profiles.get(profile_id)
            adapter = self.adapters.get(getattr(profile, "id", None)) if profile is not None else None
            if profile is None or adapter is None or not hasattr(adapter, "observe"):
                raise SafeError("profile_unavailable", "profile state could not be proven")
            value = adapter.observe(profile)
            observation = await value if inspect.isawaitable(value) else value
            if bool(getattr(observation, "running", False)):
                raise SafeError("profile_running", "profiles must be stopped before clone")

    def _stopped_sync_pair(self) -> bool:
        for profile_id in (ProfileId.TERRARIA_VANILLA.value, ProfileId.TERRARIA_TMOD.value):
            profile = self.profiles.get(profile_id)
            adapter = self.adapters.get(getattr(profile, "id", None)) if profile is not None else None
            if profile is None or adapter is None or not hasattr(adapter, "observe"):
                raise SafeError("profile_unavailable", "profile state could not be proven")
            value = adapter.observe(profile)
            if inspect.isawaitable(value):
                value = asyncio.run(value)
            if bool(getattr(value, "running", False)):
                raise SafeError("profile_running", "profiles must be stopped before clone")
        return True

    def _stopped_sync(self, profile: Any) -> bool:
        adapter = self.adapters.get(getattr(profile, "id", None)) or self.adapters.get(_rpc_key(profile))
        if adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("profile_unavailable", "profile state could not be proven")
        value = adapter.observe(profile)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        if bool(getattr(value, "running", False)):
            raise SafeError("profile_running", "profile is running; it must be stopped before clone")
        return True

    async def confirm_clone(self, action: Any, actor: str | None = None, request_id: Any = None, payload: Mapping[str, Any] | None = None, lease_check: Any = None) -> JobAccepted:
        payload = payload or {}
        await self._stopped()
        source_backup = self.service.backup_service
        def work():
            worker_db = getattr(type(source_backup.database), "open", lambda _p: None)(getattr(source_backup.database, "path", None)) if getattr(source_backup, "database", None) is not None else None
            worker_backup = BackupService(self.service.vanilla, database=worker_db, stopped_check=lambda: self._stopped_sync(self.service.vanilla), free_space=source_backup.free_space, clock=source_backup.clock, tar_runner=source_backup.tar_runner)
            worker_service = WorldService(self.service.vanilla, self.service.tmod, backup_service=worker_backup, stopped_check=lambda *_: self._stopped_sync_pair(), clock=self.service.clock, lease_check=lease_check)
            try:
                result = worker_service.clone_vanilla_to_tmod(str(payload.get("source_world_id", "")), str(payload.get("destination_name", "")), actor, request_id)
                return result, worker_db is not None
            finally:
                _close_database(worker_db)
        result, isolated = await asyncio.to_thread(work)
        if not isolated and getattr(result, "source_backup", None) is not None:
            self.service.backup_service._insert(result.source_backup)
        return JobAccepted(job_id=__import__("uuid").uuid4().hex, state="running")

    clone = confirm_clone
