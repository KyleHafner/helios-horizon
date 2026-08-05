"""Safe Vanilla-to-tModLoader world cloning."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import SafeError


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
    ) -> None:
        self.vanilla = vanilla_profile
        self.tmod = tmod_profile
        self.backup_service = backup_service
        self.stopped_check = stopped_check
        self.clock = clock or (lambda: datetime.now(timezone.utc))

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
