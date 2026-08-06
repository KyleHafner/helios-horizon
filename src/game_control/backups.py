"""Verified backups and security-first restore operations.

The services in this module deliberately do not accept filesystem paths from an
operator.  Paths come from a validated ``Profile`` and every archive member is
validated before an extraction can occur.
"""

from __future__ import annotations

import hashlib
import asyncio
import inspect
import json
import errno
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Protocol

from .errors import SafeError
from .models import BackupDestination, ProfileId


_MANIFEST = "manifest.json"
_ARCHIVE_SUFFIX = ".tar.zst"
_MARGIN = 1.10
_B2_RETENTION = 2
_B2_REMOTE = "example-b2-crypt"
_B2_CONFIG = Path("/etc/game-control/secrets.d/horizon-b2-rclone.conf")
_B2_PREFIX_ROOT = "example/horizon/app"
_BACKUP_ID = r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}"
_B2_PROFILES = frozenset(
    {
        ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value,
        ProfileId.TERRARIA_VANILLA.value,
        ProfileId.TERRARIA_TMOD.value,
    }
)
_SUNLIT_REDUNDANT_BACKUP_DIRS = frozenset({"backups"})


class BackupClass(StrEnum):
    APPLICATION = "application"
    FULL_LXC = "full-lxc"


class OnlineSaveTransport(Protocol):
    def save_off(self) -> Any: ...

    def save_all_flush(self) -> Any: ...

    def save_on(self) -> Any: ...


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size_bytes: int = 0


def b2_prefix(profile_id: str, backup_class: BackupClass = BackupClass.APPLICATION) -> str:
    profile = str(getattr(profile_id, "value", profile_id))
    try:
        backup_class = BackupClass(backup_class)
    except (TypeError, ValueError) as exc:
        raise SafeError("invalid_backup_destination", "backup class is not approved") from exc
    if profile not in _B2_PROFILES or backup_class is not BackupClass.APPLICATION:
        raise SafeError("invalid_backup_destination", "backup destination is not approved")
    return f"{_B2_PREFIX_ROOT}/{profile}"


def _validate_b2_credentials() -> None:
    """Validate the fixed credential boundary without reading its contents."""
    for directory in (
        Path("/"),
        Path("/etc"),
        Path("/etc/game-control"),
        Path("/etc/game-control/secrets.d"),
    ):
        try:
            info = os.lstat(directory)
        except OSError as exc:
            raise SafeError("backup_protection_failed", "remote backup credentials are unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or (directory == Path("/etc/game-control/secrets.d") and stat.S_IMODE(info.st_mode) != 0o700)
        ):
            raise SafeError("backup_protection_failed", "remote backup credentials are unavailable")
    try:
        info = os.lstat(_B2_CONFIG)
    except OSError as exc:
        raise SafeError("backup_protection_failed", "remote backup credentials are unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SafeError("backup_protection_failed", "remote backup credentials are unavailable")


class B2CommandTransport:
    """Fixed-scope rclone transport; credentials never enter argv or errors."""

    def __init__(
        self,
        *,
        runner: Callable[..., Any] | None = None,
        credential_validator: Callable[[], None] | None = None,
    ) -> None:
        self.runner = runner or subprocess.run
        self.credential_validator = credential_validator or _validate_b2_credentials

    def _run(self, argv: list[str]) -> Any:
        try:
            self.credential_validator()
            return self.runner(
                argv,
                check=True,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except SafeError:
            raise
        except Exception as exc:
            raise SafeError("backup_protection_failed", "remote backup operation failed") from exc

    @staticmethod
    def _approved_key(key: str) -> str:
        if not isinstance(key, str):
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        parts = key.split("/")
        if len(parts) != 5 or parts[:3] != _B2_PREFIX_ROOT.split("/"):
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        if parts[3] not in _B2_PROFILES:
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        prefix = b2_prefix(parts[3])
        if not key.startswith(prefix + "/") or not re.fullmatch(
            _BACKUP_ID + re.escape(_ARCHIVE_SUFFIX), key.removeprefix(prefix + "/")
        ):
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        return key

    @staticmethod
    def _approved_prefix(prefix: str) -> str:
        if not isinstance(prefix, str) or prefix not in {b2_prefix(profile) for profile in _B2_PROFILES}:
            raise SafeError("invalid_backup_destination", "backup destination is not approved")
        return prefix

    def _base(self, operation: str) -> list[str]:
        return ["/usr/bin/rclone", "--config", str(_B2_CONFIG), operation]

    def _remote(self, key: str) -> str:
        return f"{_B2_REMOTE}:{key}"

    def upload(self, source: Path, key: str) -> None:
        key = self._approved_key(key)
        if source.name != key.rsplit("/", 1)[-1] or not source.is_file():
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        self._run(self._base("copyto") + [str(source), self._remote(key), "--immutable"])

    def download(self, key: str, destination: Path) -> None:
        key = self._approved_key(key)
        if destination.name != key.rsplit("/", 1)[-1] or destination.exists() or not destination.parent.is_dir():
            raise SafeError("invalid_backup_destination", "backup staging path is not approved")
        self._run(self._base("copyto") + [self._remote(key), str(destination), "--immutable"])

    def verify(self, source: Path, key: str) -> None:
        key = self._approved_key(key)
        if source.name != key.rsplit("/", 1)[-1] or not source.is_file():
            raise SafeError("invalid_backup_destination", "backup object is not approved")
        prefix = key.rsplit("/", 1)[0]
        self._run(self._base("cryptcheck") + [str(source.parent), self._remote(prefix), "--one-way", "--fast-list", "--include", source.name])

    def list(self, prefix: str) -> tuple[RemoteObject, ...]:
        prefix = self._approved_prefix(prefix)
        result = self._run(self._base("lsjson") + ["--files-only", "--recursive", self._remote(prefix)])
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (AttributeError, UnicodeError, json.JSONDecodeError) as exc:
            raise SafeError("backup_protection_failed", "remote listing could not be verified") from exc
        if not isinstance(payload, list):
            raise SafeError("backup_protection_failed", "remote listing could not be verified")
        objects: list[RemoteObject] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("Name"), str):
                raise SafeError("backup_protection_failed", "remote listing could not be verified")
            name = item["Name"].lstrip("/")
            key = name if name.startswith(prefix + "/") else f"{prefix}/{name}"
            self._approved_key(key)
            objects.append(RemoteObject(key=key, size_bytes=max(0, int(item.get("Size", 0)))))
        return tuple(objects)

    def delete(self, key: str) -> None:
        self._run(self._base("deletefile") + [self._remote(self._approved_key(key))])


@dataclass(frozen=True)
class BackupRecord:
    id: str
    profile_id: str
    created_at: datetime
    size_bytes: int
    verified: bool
    protected: bool
    path: Path


@dataclass(frozen=True)
class RestoreResult:
    backup_id: str
    rollback: Path | None
    destination: Path


class BackupService:
    """Create, enumerate, protect, and prune verified profile archives."""

    def __init__(
        self,
        profile: Any,
        *,
        database: Any | None = None,
        stopped_check: Callable[[], bool] | None = None,
        free_space: Callable[[Path], int] | None = None,
        clock: Callable[[], datetime] | None = None,
        tar_runner: Callable[..., Any] | None = None,
        online_transport: OnlineSaveTransport | None = None,
    ) -> None:
        self.profile = profile
        self.database = database
        self.stopped_check = stopped_check
        self.free_space = free_space or (lambda path: shutil.disk_usage(path).free)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tar_runner = tar_runner or subprocess.run
        self.online_transport = online_transport

    @property
    def backup_root(self) -> Path:
        return Path(self.profile.paths.backup_root)

    def create(
        self,
        action: Any | None = None,
        actor: str | None = None,
        request_id: Any | None = None,
        *,
        protected: bool | None = None,
        destination: BackupDestination | str | None = None,
    ) -> BackupRecord:
        if self.stopped_check is None or not self.stopped_check():
            raise SafeError("profile_running", "profile is running; it must be stopped before backup")
        if protected is None:
            protected = bool(getattr(action, "protected", False))
        if destination is None:
            destination = getattr(action, "destination", BackupDestination.LOCAL)
        try:
            destination = BackupDestination(destination)
        except (TypeError, ValueError) as exc:
            raise SafeError("invalid_backup_destination", "backup destination is not approved") from exc
        roots = tuple(Path(root) for root in getattr(self.profile.paths, "backup_roots", ()))
        roots = roots or tuple(Path(root) for root in self.profile.paths.data_roots)
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)
        estimated = self._estimate(roots)
        if self.free_space(self.backup_root) < max(1, int(estimated * _MARGIN)):
            raise SafeError("insufficient_space", "insufficient free space for backup")

        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        backup_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
        partial = self.backup_root / f"{backup_id}.partial"
        final = self.backup_root / f"{backup_id}{_ARCHIVE_SUFFIX}"
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.backup_root))
        try:
            os.chmod(staging, 0o700)
            entries = self._snapshot(roots, staging)
            manifest = {
                "schema": 1,
                "profile_id": str(self.profile.id),
                "backup_id": backup_id,
                "created_at": now.isoformat(),
                "entries": entries,
            }
            manifest_path = staging / _MANIFEST
            _write_json_fsync(manifest_path, manifest)
            filelist = staging / ".filelist"
            paths = [_MANIFEST] + [entry["archive_path"] for entry in entries]
            _write_filelist(filelist, paths)
            argv = [
                "/usr/bin/tar",
                "--zstd",
                "--create",
                "--file",
                str(partial),
                "--directory",
                str(staging),
                "--null",
                "--files-from",
                str(filelist),
            ]
            self.tar_runner(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _fsync_file(partial)
            self._verify_archive(partial, manifest)
            os.replace(partial, final)
            _fsync_dir(self.backup_root)
            record = BackupRecord(
                id=backup_id,
                profile_id=str(self.profile.id),
                created_at=now,
                size_bytes=final.stat().st_size,
                verified=True,
                protected=bool(protected),
                path=final,
            )
            self._insert(record)
            return record
        except SafeError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            raise SafeError("backup_failed", "backup could not be verified") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def create_online(
        self,
        action: Any | None = None,
        actor: str | None = None,
        request_id: Any | None = None,
        *,
        protected: bool | None = None,
        destination: BackupDestination | str | None = None,
        max_snapshot_seconds: float = 120.0,
    ) -> BackupRecord:
        """Snapshot approved Sunlit data while save writes are quiesced."""
        if self.online_transport is None:
            raise SafeError("backup_quiesce_failed", "online backup transport is unavailable")
        profile_id = str(getattr(self.profile.id, "value", self.profile.id))
        if profile_id != ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value:
            raise SafeError("backup_quiesce_failed", "online backup is not approved for this profile")
        if protected is None:
            protected = bool(getattr(action, "protected", False))
        if destination is None:
            destination = getattr(action, "destination", BackupDestination.LOCAL)
        try:
            BackupDestination(destination)
        except (TypeError, ValueError) as exc:
            raise SafeError("invalid_backup_destination", "backup destination is not approved") from exc
        roots = tuple(Path(root) for root in getattr(self.profile.paths, "backup_roots", ()))
        roots = roots or tuple(Path(root) for root in self.profile.paths.data_roots)
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backup_root, 0o700)
        estimated = self._estimate(roots)
        if self.free_space(self.backup_root) < max(1, int(estimated * _MARGIN)):
            raise SafeError("insufficient_space", "insufficient free space for backup")

        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        backup_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
        partial = self.backup_root / f"{backup_id}.partial"
        final = self.backup_root / f"{backup_id}{_ARCHIVE_SUFFIX}"
        staging = Path(tempfile.mkdtemp(prefix=".online-stage-", dir=self.backup_root))
        failure: BaseException | None = None
        try:
            os.chmod(staging, 0o700)
            deadline = time.monotonic() + max(1.0, min(float(max_snapshot_seconds), 900.0))
            try:
                self._call_online("save_off")
                self._call_online("save_all_flush")
                entries = self._snapshot(roots, staging, copy_files=True, deadline=deadline)
            except BaseException as exc:
                failure = exc
                entries = []
            try:
                self._call_online("save_on")
            except BaseException as exc:
                failure = exc
            if failure is not None:
                raise SafeError("backup_quiesce_failed", "online backup quiesce could not be completed") from failure

            manifest = {
                "schema": 1,
                "profile_id": str(self.profile.id),
                "backup_id": backup_id,
                "created_at": now.isoformat(),
                "online_consistent": True,
                "entries": entries,
            }
            _write_json_fsync(staging / _MANIFEST, manifest)
            filelist = staging / ".filelist"
            _write_filelist(filelist, [_MANIFEST] + [entry["archive_path"] for entry in entries])
            argv = [
                "/usr/bin/tar", "--zstd", "--create", "--file", str(partial),
                "--directory", str(staging), "--null", "--files-from", str(filelist),
            ]
            self.tar_runner(argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _fsync_file(partial)
            self._verify_archive(partial, manifest)
            os.replace(partial, final)
            _fsync_dir(self.backup_root)
            record = BackupRecord(
                id=backup_id,
                profile_id=str(self.profile.id),
                created_at=now,
                size_bytes=final.stat().st_size,
                verified=True,
                protected=bool(protected),
                path=final,
            )
            self._insert(record)
            return record
        except SafeError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError) as exc:
            partial.unlink(missing_ok=True)
            raise SafeError("backup_failed", "backup could not be verified") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _call_online(self, method: str) -> Any:
        value = getattr(self.online_transport, method)()
        if inspect.isawaitable(value):
            return asyncio.run(value)
        return value

    def list(self, action: Any | None = None, actor: str | None = None, request_id: Any | None = None):
        rows = self._rows()
        records = tuple(self._record(row) for row in rows if self._record(row) is not None)
        page = getattr(action, "page", None)
        if page is not None:
            from .protocol import BackupPage

            limit = int(getattr(page, "limit", 50))
            return BackupPage(
                items=tuple(
                    __import__("game_control.protocol", fromlist=["BackupSummary"]).BackupSummary(
                        id=item.id,
                        profile_id=item.profile_id,
                        created_at=item.created_at,
                        size_bytes=item.size_bytes,
                        verified=item.verified,
                        protected=item.protected,
                    )
                    for item in records[:limit]
                ),
                next_cursor=None,
            )
        return records

    def protect(self, backup_id: str, protected: bool = True) -> BackupRecord:
        record = self._find(backup_id)
        if record is None:
            raise SafeError("backup_not_found", "backup was not found")
        if self.database is not None and hasattr(self.database, "protect_backup"):
            self.database.protect_backup(backup_id, protected)
        elif self.database is not None and hasattr(self.database, "connection"):
            self.database.connection.execute(
                "UPDATE backups SET protected = ? WHERE id = ? AND profile_id = ?",
                (int(protected), backup_id, str(self.profile.id)),
            )
            self.database.connection.commit()
        return BackupRecord(**{**record.__dict__, "protected": protected})

    def prune(self, keep: int = 2) -> tuple[BackupRecord, ...]:
        records = [record for record in self.list() if record.verified and record.path.exists()]
        records.sort(key=lambda item: item.created_at, reverse=True)
        protected = [item for item in records if item.protected]
        retained = list(protected)
        for item in records:
            if item not in retained and len(retained) < max(2, keep):
                retained.append(item)
        candidates = [item for item in records if item not in retained]
        if len(records) == 1 and candidates:
            raise SafeError("backup_retention", "refusing to delete the only verified backup")
        for item in candidates:
            item.path.unlink(missing_ok=True)
            self._delete(item.id)
        if records and not any(item.path.exists() for item in retained):
            raise SafeError("backup_retention", "refusing to delete the only verified backup")
        return tuple(sorted((item for item in retained if item.path.exists()), key=lambda x: x.created_at, reverse=True))

    def _estimate(self, roots: Iterable[Path]) -> int:
        total = 0
        for root in roots:
            if not root.exists() or root.is_symlink():
                continue
            for path in self._walk_source(root):
                try:
                    total += path.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    raise SafeError("backup_failed", "backup source could not be read") from exc
        return total + 65536

    def _walk_source(self, root: Path) -> Iterable[Path]:
        profile_id = str(getattr(self.profile.id, "value", self.profile.id))
        mutable_root = Path(self.profile.paths.mutable_root)
        excluded = (
            _SUNLIT_REDUNDANT_BACKUP_DIRS
            if profile_id == ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value and root == mutable_root
            else frozenset()
        )
        return _walk(root, excluded_top_level=excluded)

    def _snapshot(
        self,
        roots: tuple[Path, ...],
        staging: Path,
        *,
        copy_files: bool = False,
        deadline: float | None = None,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for index, root in enumerate(roots):
            if root.is_symlink():
                raise SafeError("backup_failed", "profile data root is a symlink")
            payload_root = staging / "payload" / str(index)
            payload_root.mkdir(parents=True, mode=0o700)
            if not root.exists():
                continue
            for source in self._walk_source(root):
                _check_deadline(deadline)
                relative = source.relative_to(root)
                if any(part.endswith(".partial") for part in relative.parts):
                    continue
                if source.is_symlink():
                    continue
                destination = payload_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    if copy_files:
                        digest = _stage_file_copy(source, destination, deadline=deadline)
                        _check_deadline(deadline)
                        os.chmod(destination, 0o400)
                        metadata_path = destination
                    else:
                        _stage_file(source, destination)
                        metadata_path = source
                        digest = _sha256(source)
                    source_info = metadata_path.stat(follow_symlinks=False)
                except OSError as exc:
                    raise SafeError("backup_failed", "backup source could not be read") from exc
                entries.append(
                    {
                        "path": relative.as_posix() if len(roots) == 1 else f"{index}/{relative.as_posix()}",
                        "archive_path": f"payload/{index}/{relative.as_posix()}",
                        "size": source_info.st_size,
                        "mode": stat.S_IMODE(source_info.st_mode),
                        "uid": source_info.st_uid,
                        "gid": source_info.st_gid,
                        "sha256": digest,
                    }
                )
        entries.sort(key=lambda item: item["path"])
        return entries

    def _verify_archive(self, archive_path: Path, manifest: dict[str, Any]) -> None:
        try:
            with tarfile.open(archive_path, mode="r:*") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                if _MANIFEST not in names:
                    raise SafeError("backup_failed", "backup manifest is missing")
                expected = {_MANIFEST} | {entry["archive_path"] for entry in manifest["entries"]}
                if names != expected:
                    raise SafeError("backup_failed", "backup archive contents changed")
                for entry in manifest["entries"]:
                    member = archive.getmember(entry["archive_path"])
                    if not member.isfile():
                        raise SafeError("backup_failed", "backup contains an invalid member")
                    stream = archive.extractfile(member)
                    if stream is None or _hash_stream(stream) != entry["sha256"]:
                        raise SafeError("backup_failed", "backup checksum verification failed")
        except tarfile.TarError:
            # Python builds without libzstd use one fixed decompressor process
            # and consume its tar stream sequentially; never spawn per-file
            # extractors for large worlds.
            _verify_zstd_stream(archive_path, manifest)
        except (KeyError, TypeError) as exc:
            raise SafeError("backup_failed", "backup archive could not be verified") from exc

    def _insert(self, record: BackupRecord) -> None:
        if self.database is None:
            return
        row = {
            "id": record.id,
            "profile_id": record.profile_id,
            "created_at": record.created_at.isoformat(),
            "size_bytes": record.size_bytes,
            "verified": record.verified,
            "protected": record.protected,
            "path": str(record.path),
        }
        if hasattr(self.database, "insert_backup"):
            self.database.insert_backup(**row)
        elif hasattr(self.database, "connection"):
            self.database.connection.execute(
                "INSERT INTO backups(id, profile_id, created_at, size_bytes, verified, protected) VALUES(?,?,?,?,?,?)",
                (
                    record.id,
                    record.profile_id,
                    record.created_at.isoformat(),
                    record.size_bytes,
                    int(record.verified),
                    int(record.protected),
                ),
            )
            self.database.connection.commit()

    def _rows(self) -> list[Any]:
        if self.database is not None and hasattr(self.database, "list_backups"):
            return list(self.database.list_backups(str(self.profile.id)))
        if self.database is not None and hasattr(self.database, "connection"):
            return list(
                self.database.connection.execute(
                    "SELECT id, profile_id, created_at, size_bytes, verified, protected FROM backups WHERE profile_id = ? ORDER BY created_at DESC",
                    (str(self.profile.id),),
                )
            )
        return [
            {
                "id": path.name.removesuffix(_ARCHIVE_SUFFIX),
                "profile_id": str(self.profile.id),
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                "size_bytes": path.stat().st_size,
                "verified": True,
                "protected": False,
                "path": str(path),
            }
            for path in self.backup_root.glob(f"*{_ARCHIVE_SUFFIX}")
        ]

    def _record(self, row: Any) -> BackupRecord | None:
        if isinstance(row, BackupRecord):
            return row
        if hasattr(row, "keys"):
            value = row
            get = value.__getitem__
        else:
            names = ("id", "profile_id", "created_at", "size_bytes", "verified", "protected", "path")
            value = dict(zip(names, row))
            get = value.__getitem__
        path_value = value.get("path") if hasattr(value, "get") else None
        path = Path(path_value) if path_value else self.backup_root / f"{get('id')}{_ARCHIVE_SUFFIX}"
        created = get("created_at")
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return BackupRecord(
            id=str(get("id")),
            profile_id=str(get("profile_id")),
            created_at=created,
            size_bytes=int(get("size_bytes")),
            verified=bool(get("verified")),
            protected=bool(get("protected")),
            path=path,
        )

    def _find(self, backup_id: str) -> BackupRecord | None:
        return next((item for item in self.list() if item.id == backup_id), None)

    def _delete(self, backup_id: str) -> None:
        if self.database is not None and hasattr(self.database, "delete_backup"):
            self.database.delete_backup(backup_id)
        elif self.database is not None and hasattr(self.database, "connection"):
            self.database.connection.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
            self.database.connection.commit()


class RestoreService:
    def __init__(
        self,
        profile: Any,
        *,
        backup_service: BackupService,
        stopped_check: Callable[[], bool] | None = None,
        free_space: Callable[[Path], int] | None = None,
        health_check: Callable[[Path], bool] | None = None,
    ) -> None:
        self.profile = profile
        self.backup_service = backup_service
        self.stopped_check = stopped_check
        self.free_space = free_space or (lambda path: shutil.disk_usage(path).free)
        self.health_check = health_check

    def restore(
        self,
        archive: str | os.PathLike[str],
        actor: str | None = None,
        request_id: Any | None = None,
        **_: Any,
    ) -> RestoreResult:
        if self.stopped_check is None or not self.stopped_check():
            raise SafeError("profile_running", "profile is running; it must be stopped before restore")
        archive_path = Path(archive)
        root = Path(self.profile.paths.backup_root)
        if archive_path.is_symlink() or not _within(archive_path, root):
            raise SafeError("invalid_backup", "backup archive is not approved")
        if not archive_path.is_file():
            raise SafeError("backup_not_found", "backup archive was not found")
        try:
            try:
                source: Any = tarfile.open(archive_path, mode="r:*")
                external = False
            except tarfile.TarError:
                source = _ExternalArchive(archive_path)
                external = True
            with source:
                members, manifest = (
                    self._validate_external(source)
                    if external
                    else self._validate_archive(source)
                )
                required = sum(int(item.get("size", 0)) for item in manifest["entries"]) + 65536
                destination_parent = Path(self.profile.paths.mutable_root).parent
                if self.free_space(destination_parent) < int(required * _MARGIN):
                    raise SafeError("insufficient_space", "insufficient free space for restore")
                # Validation is complete before the pre-restore backup or any
                # destination path is created.
                self.backup_service.create(protected=True)
                staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination_parent))
                os.chmod(staging, 0o700)
                try:
                    if external:
                        self._extract_external(source, manifest, staging)
                    else:
                        self._extract(source, members, manifest, staging)
                    os.chmod(staging, 0o750)
                    os.chown(
                        staging,
                        int(getattr(self.profile, "owner_uid", os.geteuid())),
                        int(getattr(self.profile, "owner_gid", os.getegid())),
                    )
                    destination = Path(self.profile.paths.mutable_root)
                    if destination.is_symlink():
                        raise SafeError("invalid_destination", "restore destination is a symlink")
                    rollback = destination_parent / f".rollback-{uuid.uuid4().hex}"
                    if destination.exists():
                        os.replace(destination, rollback)
                    try:
                        os.replace(staging, destination)
                    except Exception:
                        if rollback.exists():
                            os.replace(rollback, destination)
                        raise
                    result = RestoreResult(
                        backup_id=str(manifest["backup_id"]),
                        rollback=rollback if rollback.exists() else None,
                        destination=destination,
                    )
                    if self.health_check is not None and self.health_check(destination):
                        self.finalize(result)
                    return result
                except SafeError:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
                finally:
                    if staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)
        except SafeError:
            raise
        except (OSError, tarfile.TarError, ValueError, KeyError) as exc:
            raise SafeError("restore_failed", "restore could not be completed") from exc

    def finalize(self, result: RestoreResult) -> None:
        if result.rollback is not None:
            shutil.rmtree(result.rollback, ignore_errors=True)

    def _validate_archive(self, archive: tarfile.TarFile) -> tuple[list[tarfile.TarInfo], dict[str, Any]]:
        members = archive.getmembers()
        if len({member.name for member in members}) != len(members):
            raise SafeError("invalid_backup", "backup contains duplicate members")
        for member in members:
            _validate_member(member)
        manifest_member = next((member for member in members if member.name == _MANIFEST), None)
        if manifest_member is None:
            raise SafeError("invalid_backup", "backup manifest is missing")
        stream = archive.extractfile(manifest_member)
        if stream is None:
            raise SafeError("invalid_backup", "backup manifest is invalid")
        try:
            manifest = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SafeError("invalid_backup", "backup manifest is invalid") from exc
        if manifest.get("schema") != 1 or manifest.get("profile_id") != str(self.profile.id):
            raise SafeError("wrong_profile", "backup belongs to another profile")
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise SafeError("invalid_backup", "backup manifest is invalid")
        if any(not isinstance(entry, dict) for entry in entries):
            raise SafeError("invalid_backup", "backup manifest is invalid")
        by_name = {member.name: member for member in members}
        expected_uid = int(getattr(self.profile, "owner_uid", os.geteuid()))
        expected_gid = int(getattr(self.profile, "owner_gid", os.getegid()))
        for entry in entries:
            archive_name = entry.get("archive_path")
            if not isinstance(archive_name, str) or archive_name not in by_name:
                raise SafeError("invalid_backup", "backup manifest is invalid")
            relative_path = entry.get("path")
            if (
                not isinstance(relative_path, str)
                or not relative_path
                or "\x00" in relative_path
                or PurePosixPath(relative_path).is_absolute()
                or ".." in PurePosixPath(relative_path).parts
            ):
                raise SafeError("invalid_backup", "backup manifest contains an unsafe path")
            member = by_name[archive_name]
            if member.issym() or member.islnk() or not member.isfile():
                raise SafeError("invalid_backup", "backup contains an invalid member")
            if archive_name == _MANIFEST or int(member.uid) != expected_uid or int(member.gid) != expected_gid:
                raise SafeError("ownership_mismatch", "backup ownership does not match profile")
            if int(entry.get("uid", -1)) != expected_uid or int(entry.get("gid", -1)) != expected_gid:
                raise SafeError("ownership_mismatch", "backup ownership does not match profile")
            if int(entry.get("size", -1)) != int(member.size):
                raise SafeError("checksum_mismatch", "backup size verification failed")
            content = archive.extractfile(member)
            if content is None or _hash_stream(content) != entry.get("sha256"):
                raise SafeError("checksum_mismatch", "backup checksum verification failed")
        if (
            len({entry["archive_path"] for entry in entries}) != len(entries)
            or any(entry["archive_path"] == _MANIFEST for entry in entries)
            or set(by_name) != {_MANIFEST} | {entry["archive_path"] for entry in entries}
        ):
            raise SafeError("invalid_backup", "backup manifest does not match archive")
        if not isinstance(manifest.get("backup_id"), str) or not manifest["backup_id"]:
            raise SafeError("invalid_backup", "backup manifest is invalid")
        return members, manifest

    def _extract(self, archive: tarfile.TarFile, members: list[tarfile.TarInfo], manifest: dict[str, Any], staging: Path) -> None:
        entries = {entry["archive_path"]: entry for entry in manifest["entries"]}
        for member in members:
            if member.name == _MANIFEST:
                continue
            entry = entries[member.name]
            target = staging / PurePosixPath(entry["path"])
            if not _within(target, staging):
                raise SafeError("invalid_backup", "backup path escapes destination")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            source = archive.extractfile(member)
            if source is None:
                raise SafeError("restore_failed", "backup member could not be read")
            with target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
            os.chmod(target, 0o640)
            os.chown(target, int(getattr(self.profile, "owner_uid", os.geteuid())), int(getattr(self.profile, "owner_gid", os.getegid())))

    def _validate_external(self, archive: "_ExternalArchive") -> tuple[list["_ExternalMember"], dict[str, Any]]:
        members = archive.members
        if len({member.name for member in members}) != len(members):
            raise SafeError("invalid_backup", "backup contains duplicate members")
        by_name = {member.name: member for member in members}
        if _MANIFEST not in by_name:
            raise SafeError("invalid_backup", "backup manifest is missing")
        try:
            manifest = json.loads(archive.read(_MANIFEST))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SafeError("invalid_backup", "backup manifest is invalid") from exc
        if manifest.get("schema") != 1 or manifest.get("profile_id") != str(self.profile.id):
            raise SafeError("wrong_profile", "backup belongs to another profile")
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            raise SafeError("invalid_backup", "backup manifest is invalid")
        if any(not isinstance(entry, dict) for entry in entries):
            raise SafeError("invalid_backup", "backup manifest is invalid")
        expected_uid = int(getattr(self.profile, "owner_uid", os.geteuid()))
        expected_gid = int(getattr(self.profile, "owner_gid", os.getegid()))
        for entry in entries:
            archive_name = entry.get("archive_path")
            relative_path = entry.get("path")
            if (
                not isinstance(archive_name, str)
                or archive_name not in by_name
                or not isinstance(relative_path, str)
                or not relative_path
                or PurePosixPath(relative_path).is_absolute()
                or ".." in PurePosixPath(relative_path).parts
            ):
                raise SafeError("invalid_backup", "backup manifest contains an unsafe path")
            member = by_name[archive_name]
            if not member.isfile:
                raise SafeError("invalid_backup", "backup contains an invalid member")
            if member.uid != expected_uid or member.gid != expected_gid:
                raise SafeError("ownership_mismatch", "backup ownership does not match profile")
            if int(entry.get("uid", -1)) != expected_uid or int(entry.get("gid", -1)) != expected_gid:
                raise SafeError("ownership_mismatch", "backup ownership does not match profile")
            if member.size != int(entry.get("size", -1)):
                raise SafeError("checksum_mismatch", "backup size verification failed")
            with archive.open(archive_name) as content:
                digest = _hash_stream(content)
            if digest != entry.get("sha256"):
                raise SafeError("checksum_mismatch", "backup checksum verification failed")
        if (
            len({entry.get("archive_path") for entry in entries}) != len(entries)
            or any(entry.get("archive_path") == _MANIFEST for entry in entries)
            or set(by_name) != {_MANIFEST} | {entry.get("archive_path") for entry in entries}
        ):
            raise SafeError("invalid_backup", "backup manifest does not match archive")
        if not isinstance(manifest.get("backup_id"), str) or not manifest["backup_id"]:
            raise SafeError("invalid_backup", "backup manifest is invalid")
        return members, manifest

    def _extract_external(self, archive: "_ExternalArchive", manifest: dict[str, Any], staging: Path) -> None:
        for entry in manifest["entries"]:
            target = staging / PurePosixPath(entry["path"])
            if not _within(target, staging):
                raise SafeError("invalid_backup", "backup path escapes destination")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
            with archive.open(entry["archive_path"]) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            os.chmod(target, 0o640)
            os.chown(
                target,
                int(getattr(self.profile, "owner_uid", os.geteuid())),
                int(getattr(self.profile, "owner_gid", os.getegid())),
            )


def _walk(root: Path, *, excluded_top_level: frozenset[str] = frozenset()) -> Iterable[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as scan:
                children = sorted(scan, key=lambda item: item.name, reverse=True)
                for item in children:
                    if item.name.endswith(".partial"):
                        continue
                    if current == root and item.name in excluded_top_level:
                        continue
                    path = Path(item.path)
                    if item.is_symlink():
                        continue
                    if item.is_dir(follow_symlinks=False):
                        stack.append(path)
                    elif item.is_file(follow_symlinks=False):
                        yield path
        except OSError as exc:
            raise SafeError("backup_failed", "backup source could not be read") from exc


def _stage_file(source: Path, destination: Path) -> None:
    """Snapshot a stopped source without copying multi-gigabyte worlds."""
    try:
        os.link(source, destination, follow_symlinks=False)
    except (TypeError, NotImplementedError):
        shutil.copy2(source, destination, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM):
            raise
        shutil.copy2(source, destination, follow_symlinks=False)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise SafeError("backup_quiesce_timeout", "online backup staging exceeded its bound")


def _stage_file_copy(source: Path, destination: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream, destination.open("wb") as output_stream:
            while True:
                _check_deadline(deadline)
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
                digest.update(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except OSError as exc:
        raise SafeError("backup_failed", "backup source could not be read") from exc
    return digest.hexdigest()


def _validate_member(member: tarfile.TarInfo) -> None:
    name = member.name
    path = PurePosixPath(name)
    if not name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise SafeError("invalid_backup", "backup contains an unsafe path")
    if member.issym() or member.islnk() or member.isdev() or member.isfifo() or member.ischr() or member.isblk():
        raise SafeError("invalid_backup", "backup contains an unsafe member")
    if not member.isfile() and name != _MANIFEST:
        raise SafeError("invalid_backup", "backup contains an unsupported member")


def _within(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return _hash_stream(stream)


def _hash_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _write_json_fsync(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_filelist(path: Path, values: Iterable[str]) -> None:
    with path.open("wb") as stream:
        for value in values:
            stream.write(value.encode() + b"\0")
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_zstd_stream(archive_path: Path, manifest: dict[str, Any]) -> None:
    expected = {_MANIFEST} | {entry["archive_path"] for entry in manifest["entries"]}
    entries = {entry["archive_path"]: entry for entry in manifest["entries"]}
    process: subprocess.Popen[bytes] | None = None
    seen: set[str] = set()
    try:
        process = subprocess.Popen(
            ["/usr/bin/zstd", "-dc", "--", str(archive_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None:
            raise SafeError("backup_failed", "backup decompressor was unavailable")
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if len(seen) >= 1_000_000 or len(member.name) > 4096:
                    raise SafeError("backup_failed", "backup archive is too large")
                if member.name in seen:
                    raise SafeError("backup_failed", "backup contains duplicate members")
                seen.add(member.name)
                _validate_member(member)
                if member.name == _MANIFEST:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise SafeError("backup_failed", "backup manifest is missing")
                    json.loads(stream.read())
                    continue
                entry = entries.get(member.name)
                if entry is None or not member.isfile() or int(member.size) != int(entry["size"]):
                    raise SafeError("backup_failed", "backup archive contents changed")
                stream = archive.extractfile(member)
                if stream is None or _hash_stream(stream) != entry["sha256"]:
                    raise SafeError("backup_failed", "backup checksum verification failed")
        returncode = process.wait()
        if returncode != 0:
            raise SafeError("backup_failed", "backup decompression failed")
        if seen != expected:
            raise SafeError("backup_failed", "backup archive contents changed")
    except SafeError:
        if process is not None and process.poll() is None:
            process.kill()
        if process is not None:
            process.wait()
        raise
    except (OSError, subprocess.SubprocessError, tarfile.TarError, json.JSONDecodeError) as exc:
        if process is not None and process.poll() is None:
            process.kill()
        if process is not None:
            process.wait()
        raise SafeError("backup_failed", "backup archive could not be verified") from exc


@dataclass(frozen=True)
class _ExternalMember:
    name: str
    isfile: bool
    size: int
    uid: int
    gid: int
    staged_path: Path | None


class _ExternalArchive:
    """Read a zstd tar stream once for Python builds lacking native zstd."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._staging = Path(tempfile.mkdtemp(prefix=".zstd-", dir=path.parent))
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                ["/usr/bin/zstd", "-dc", "--", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None:
                raise SafeError("invalid_backup", "backup decompressor was unavailable")
            members: list[_ExternalMember] = []
            seen: set[str] = set()
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    if len(seen) >= 1_000_000 or len(member.name) > 4096:
                        raise SafeError("invalid_backup", "backup archive is too large")
                    if member.name in seen:
                        raise SafeError("invalid_backup", "backup contains duplicate members")
                    seen.add(member.name)
                    _validate_member(member)
                    staged_path: Path | None = None
                    if member.isfile():
                        staged_path = self._staging / str(len(members))
                        with staged_path.open("wb") as output:
                            source = archive.extractfile(member)
                            if source is None:
                                raise SafeError("invalid_backup", "backup member could not be read")
                            shutil.copyfileobj(source, output)
                    members.append(
                        _ExternalMember(
                            member.name,
                            member.isfile(),
                            int(member.size),
                            int(member.uid),
                            int(member.gid),
                            staged_path,
                        )
                    )
            if process.wait() != 0:
                raise SafeError("invalid_backup", "backup decompression failed")
            self.members = tuple(members)
        except SafeError:
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()
            self.close()
            raise
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            if process is not None and process.poll() is None:
                process.kill()
            if process is not None:
                process.wait()
            self.close()
            raise SafeError("invalid_backup", "backup archive could not be read") from exc

    def __enter__(self) -> "_ExternalArchive":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def open(self, name: str):
        member = next((item for item in self.members if item.name == name), None)
        if member is None or member.staged_path is None:
            raise SafeError("invalid_backup", "backup member could not be read")
        return member.staged_path.open("rb")

    def read(self, name: str) -> bytes:
        with self.open(name) as source:
            return source.read()

    def close(self) -> None:
        shutil.rmtree(self._staging, ignore_errors=True)
