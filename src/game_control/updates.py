"""Fixed, root-profile-selected update strategies with explicit rollback."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .adapters.crafty import parse_version_text
from .errors import SafeError
from .interim_maintenance_control import maintenance_argv
from .protocol import UpdateStatus

STEAMCMD_ARGV = (
    "/opt/steamcmd/steamcmd.sh",
    "+force_install_dir",
    "/opt/pzserver",
    "+login",
    "anonymous",
    "+app_update",
    "380870",
    "-beta",
    "unstable",
    "validate",
    "+quit",
)

_MAX_CANDIDATE_URL_LENGTH = 256
_UNKNOWN_VERSION = "unknown"
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


def _id(profile: Any) -> str:
    value = getattr(profile, "id", profile)
    return getattr(value, "value", str(value))


@dataclass(frozen=True)
class UpdateResult:
    state: str
    profile_id: str
    strategy: str
    prior_version: str | None = None
    new_version: str | None = None


class UpdateService:
    def __init__(
        self,
        profiles: Mapping[str, Any] | Any,
        *,
        database: Any | None = None,
        backup_service: Any | None = None,
        runner: Callable[..., Any] | None = None,
        downloader: Callable[..., Any] | None = None,
        stage_release: Callable[..., Any] | None = None,
        verify_release: Callable[..., bool] | None = None,
        stopped_check: Callable[[Any], bool] | Callable[[], bool] | None = None,
        running_check: Callable[[Any], bool] | Callable[[], bool] | None = None,
        http_client: Any | None = None,
        clock: Callable[[], float] | None = None,
        lease_check: Callable[[], bool] | None = None,
    ) -> None:
        if isinstance(profiles, Mapping):
            self.profiles = profiles
        elif hasattr(profiles, "id"):
            self.profiles = {_id(profiles): profiles}
        else:
            self.profiles = {_id(item): item for item in profiles}
        self.database = database
        self.backup_service = backup_service
        self.runner = runner or subprocess.run
        self.downloader = downloader
        self.stage_release = stage_release
        self.verify_release = verify_release
        self.stopped_check = stopped_check
        self.running_check = running_check
        self.http_client = http_client or httpx.Client(timeout=10.0)
        self.clock = clock or time.time
        self.lease_check = lease_check

    def _assert_lease(self) -> None:
        if self.lease_check is not None and not self.lease_check():
            raise SafeError("slot_conflict", "operation lease was lost before publication")

    def _profile(self, value: Any) -> Any:
        key = _id(getattr(value, "profile_id", value))
        try:
            return self.profiles[key]
        except (KeyError, TypeError) as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc

    def _is_running(self, profile: Any) -> bool:
        check = self.running_check
        if check is not None:
            try:
                return bool(check(profile))
            except TypeError:
                return bool(check())
        if self.stopped_check is not None:
            try:
                return not bool(self.stopped_check(profile))
            except TypeError:
                return not bool(self.stopped_check())
        state = getattr(profile, "state", None)
        return str(getattr(state, "value", state)) in {"running", "starting", "stopping"}

    def _strategy(self, profile: Any) -> str:
        return str(profile.update.kind)

    def check(self, profile: Any) -> UpdateStatus:
        profile_obj = self._profile(profile)
        strategy = self._strategy(profile_obj)
        installed = self._installed_version(profile_obj)
        available = self._candidate_version(profile_obj)
        if available == installed:
            available = None
        return UpdateStatus(
            profile_id=profile_obj.id,
            strategy=strategy,
            installed_version=installed,
            available_version=available,
            restart_required=strategy != "manual",
            apply_supported=strategy != "manual",
        )

    @staticmethod
    def _candidate_version(profile: Any) -> str | None:
        """Return a bounded candidate version only for fixed release URLs."""
        try:
            strategy = str(profile.update.kind)
            if strategy == "curated_modpack":
                value = str(profile.update.curated.version)
                return value if 1 <= len(value) <= 128 else None
            if strategy != "release_symlink":
                return None
            raw_url = profile.update.download_url
            if raw_url is None:
                return None
            url = str(raw_url)
            if len(url) > _MAX_CANDIDATE_URL_LENGTH:
                return None
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path:
                return None
            version = parse_version_text(parsed.path)
            return None if version in {None, _UNKNOWN_VERSION} else version
        except Exception:
            return None

    def _installed_version(self, profile: Any) -> str | None:
        if self._strategy(profile) == "curated_modpack":
            try:
                target = os.readlink(Path(profile.update.curated.active_link))
                return Path(target).name[:128] or None
            except OSError:
                return None
        path = Path(profile.paths.version_file)
        try:
            return parse_version_text(path.read_text(encoding="utf-8"))
        except OSError:
            current = Path(profile.paths.install_root) / "current"
            try:
                return Path(os.readlink(current)).name
            except OSError:
                return None

    def _backup(self, profile: Any) -> Any:
        service = self.backup_service
        if service is None:
            raise SafeError("backup_failed", "verified pre-update backup is required")
        record = None
        create = getattr(service, "create", None)
        if create is not None:
            try:
                record = create(protected=True)
            except TypeError:
                try:
                    record = create(profile, protected=True)
                except TypeError:
                    record = create()
            except SafeError:
                raise
            except Exception as exc:
                raise SafeError("backup_failed", "verified pre-update backup is required") from exc
        if record is None:
            records = getattr(service, "list", lambda: ())()
            record = next((item for item in records if bool(getattr(item, "verified", False))), None)
        verified = record.get("verified", False) if isinstance(record, Mapping) else getattr(record, "verified", False)
        if record is None or not bool(verified):
            raise SafeError("backup_failed", "verified pre-update backup is required")
        path = record.get("path") if isinstance(record, Mapping) else getattr(record, "path", None)
        if path is not None and (not Path(path).is_file() or Path(path).stat().st_size <= 0):
            raise SafeError("backup_failed", "verified pre-update backup is required")
        return record

    def _record(self, profile: Any, state: str, prior: str | None, new: str | None) -> None:
        connection = getattr(self.database, "connection", self.database)
        if connection is None:
            return
        try:
            connection.execute(
                "INSERT INTO updates(id,profile_id,created_at,strategy,prior_version,new_version,state)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    _id(profile),
                    datetime.now(timezone.utc).isoformat(),
                    self._strategy(profile),
                    prior,
                    new,
                    state,
                ),
            )
            connection.commit()
        except Exception:
            # State recording must never turn a successful update into an
            # unsafe partial rollback; production schema is created by StateDB.
            pass

    def _run(self, argv: list[str] | tuple[str, ...], **kwargs: Any) -> Any:
        kwargs.setdefault("check", True)
        kwargs.setdefault("shell", False)
        return self.runner(maintenance_argv(argv, slice_name="maintenance.slice"), **kwargs)

    def apply(self, profile: Any) -> UpdateResult:
        profile_obj = self._profile(profile)
        if self._is_running(profile_obj):
            raise SafeError("profile_running", "profile is running; it must be stopped before update")
        strategy = self._strategy(profile_obj)
        if strategy == "manual":
            return UpdateResult("manual_only", _id(profile_obj), strategy)
        backup = self._backup(profile_obj)
        if strategy == "steamcmd_in_place":
            try:
                self._run(STEAMCMD_ARGV)
            except Exception as exc:
                self._record(profile_obj, "failed", None, None)
                raise SafeError("update_failed", "update failed") from exc
            self._record(profile_obj, "succeeded", None, None)
            return UpdateResult("succeeded", _id(profile_obj), strategy)
        if strategy == "release_symlink":
            return self._apply_release(profile_obj, backup)
        raise SafeError("update_failed", "update strategy is unavailable")

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _fsync_tree(cls, root: Path) -> None:
        """Make every regular extracted file and directory durable bottom-up."""
        entries = sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True)
        for path in entries:
            if path.is_symlink():
                continue
            if path.is_file():
                cls._fsync_file(path)
            elif path.is_dir():
                cls._fsync_dir(path)
        cls._fsync_dir(root)

    def _trusted_checksum(self, profile: Any) -> str:
        configured = getattr(profile.update, "sha256", None)
        if not isinstance(configured, str) or not _SHA256_RE.fullmatch(configured):
            raise SafeError("update_failed", "trusted release checksum is required")
        return configured.lower()

    def _download(self, profile: Any, destination: Path) -> None:
        # Resolve trust before invoking any user-supplied downloader or network
        # client. A missing/invalid digest must not cause an untrusted fetch.
        expected_checksum = self._trusted_checksum(profile)
        if self.downloader is not None:
            try:
                self.downloader(profile, destination)
            except TypeError:
                self.downloader(str(profile.update.download_url), destination)
        else:
            with self.http_client.stream("GET", str(profile.update.download_url), timeout=10.0) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    total = 0
                    for chunk in response.iter_bytes():
                        if chunk:
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise SafeError("update_failed", "release download exceeds size limit")
                            output.write(chunk)
        try:
            if destination.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise SafeError("update_failed", "release download exceeds size limit")
        except OSError as exc:
            raise SafeError("update_failed", "release download is unavailable") from exc
        self._fsync_file(destination)
        self._fsync_dir(destination.parent)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), expected_checksum):
            raise SafeError("update_failed", "release checksum verification failed")

    def _safe_extract(self, archive_path: Path, staging: Path, expected_relative: str) -> None:
        archive_size = archive_path.stat().st_size
        try:
            free = shutil.disk_usage(staging).free
            if free < MAX_EXTRACTED_BYTES + MAX_MEMBER_BYTES:
                raise SafeError("update_failed", "insufficient free space for release extraction")
        except OSError as exc:
            raise SafeError("update_failed", "unable to verify release free space") from exc
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                members = archive.infolist()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise SafeError("update_failed", "release archive has too many members")
                total = 0
                for member in members:
                    if member.file_size > MAX_MEMBER_BYTES:
                        raise SafeError("update_failed", "release archive member is too large")
                    total += member.file_size
                    if total > MAX_EXTRACTED_BYTES or (archive_size and total > archive_size * MAX_COMPRESSION_RATIO):
                        raise SafeError("update_failed", "release archive expansion exceeds limit")
                    target = (staging / member.filename).resolve()
                    if staging.resolve() not in target.parents and target != staging.resolve():
                        raise SafeError("update_failed", "release archive is invalid")
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise SafeError("update_failed", "release archive is invalid")
                    archive.extract(member, staging)
            return
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                members = archive.getmembers()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise SafeError("update_failed", "release archive has too many members")
                total = 0
                for member in members:
                    if member.size > MAX_MEMBER_BYTES:
                        raise SafeError("update_failed", "release archive member is too large")
                    total += member.size
                    if total > MAX_EXTRACTED_BYTES or (archive_size and total > archive_size * MAX_COMPRESSION_RATIO):
                        raise SafeError("update_failed", "release archive expansion exceeds limit")
                    target = (staging / member.name).resolve()
                    if staging.resolve() not in target.parents and target != staging.resolve():
                        raise SafeError("update_failed", "release archive is invalid")
                    if member.issym() or member.islnk():
                        raise SafeError("update_failed", "release archive is invalid")
                archive.extractall(staging)
        except tarfile.TarError:
            # A single executable payload is also accepted by fixed profiles.
            target = staging / expected_relative
            with archive_path.open("rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            self._fsync_file(target)
            self._fsync_dir(target.parent)

    def _apply_release(self, profile: Any, backup: Any) -> UpdateResult:
        install = Path(profile.paths.install_root)
        releases = install / "releases"
        releases.mkdir(parents=True, exist_ok=True, mode=0o750)
        current = install / "current"
        prior_target: str | None = os.readlink(current) if current.is_symlink() else None
        prior_version = Path(prior_target).name if prior_target else None
        staging = Path(tempfile.mkdtemp(prefix=".release-", dir=install))
        new_version: str | None = None
        swapped = False
        try:
            if self.stage_release is not None:
                try:
                    value = self.stage_release(profile, staging)
                except TypeError:
                    value = self.stage_release(staging)
                new_version = str(value) if value else None
            else:
                payload = staging / ".download"
                self._download(profile, payload)
                self._safe_extract(payload, staging, str(profile.update.executable_relative_path))
                self._fsync_tree(staging)
            relative = str(profile.update.executable_relative_path)
            if ".." in Path(relative).parts:
                raise SafeError("update_failed", "release executable is invalid")
            executable = staging / relative
            if not executable.is_file():
                raise SafeError("update_failed", "release executable verification failed")
            if profile.update.version_command:
                command = [
                    (str(argument).replace("{staged_executable}", str(executable))
                     if "{staged_executable}" in str(argument) else str(executable)
                     if "/current/" in str(argument) else argument)
                    for argument in profile.update.version_command
                ]
                if not any(str(argument) == str(executable) for argument in command):
                    command.append(str(executable))
                completed = subprocess.run(command, check=True, capture_output=True, text=True, shell=False)
                if not completed.stdout.strip():
                    raise SafeError("update_failed", "release version verification failed")
                if new_version is None:
                    new_version = completed.stdout.strip().splitlines()[0][:128]
            if new_version is None:
                new_version = f"release-{int(self.clock())}"
            destination = releases / new_version
            if destination.exists():
                destination = releases / f"{new_version}-{uuid.uuid4().hex[:8]}"
            self._assert_lease()
            os.replace(staging, destination)
            self._fsync_dir(releases)
            staging = destination
            temporary_link = install / f".current-{uuid.uuid4().hex}"
            os.symlink(os.path.relpath(destination, install), temporary_link, target_is_directory=True)
            self._fsync_dir(install)
            self._assert_lease()
            os.replace(temporary_link, current)
            swapped = True
            self._fsync_dir(install)
            if self.verify_release is not None and not self.verify_release(profile, destination):
                self._record(profile, "failed", prior_version, new_version)
                self._rollback_link(current, prior_target)
                swapped = False
                self._record(profile, "rolled_back", new_version, prior_version)
                raise SafeError("update_failed", "release verification failed; update rolled back")
            self._record(profile, "succeeded", prior_version, new_version)
            return UpdateResult("succeeded", _id(profile), "release_symlink", prior_version, new_version)
        except SafeError:
            if swapped:
                self._rollback_link(current, prior_target)
            if staging.name.startswith(".release-"):
                shutil.rmtree(staging, ignore_errors=True)
            raise
        except BaseException as exc:
            if swapped:
                self._rollback_link(current, prior_target)
                if isinstance(exc, Exception):
                    self._record(profile, "failed", prior_version, new_version)
                    self._record(profile, "rolled_back", new_version, prior_version)
            if staging.name.startswith(".release-"):
                shutil.rmtree(staging, ignore_errors=True)
            if isinstance(exc, BaseException) and not isinstance(exc, Exception):
                raise
            raise SafeError("update_failed", "release update failed") from exc

    @staticmethod
    def _rollback_link(current: Path, prior_target: str | None) -> None:
        temporary = current.parent / f".rollback-{uuid.uuid4().hex}"
        if prior_target is None:
            current.unlink(missing_ok=True)
            UpdateService._fsync_dir(current.parent)
        else:
            os.symlink(prior_target, temporary, target_is_directory=True)
            UpdateService._fsync_dir(current.parent)
            os.replace(temporary, current)
            UpdateService._fsync_dir(current.parent)

    def rollback(self, profile: Any, target: str | None = None) -> UpdateResult:
        profile_obj = self._profile(profile)
        current = Path(profile_obj.paths.install_root) / "current"
        if target is None:
            raise SafeError("update_failed", "rollback target is unavailable")
        target_path = Path(profile_obj.paths.install_root) / "releases" / target
        if not target_path.is_dir() or target_path.is_symlink():
            raise SafeError("update_failed", "rollback target is unavailable")
        self._rollback_link(current, os.path.relpath(target_path, current.parent))
        self._record(profile_obj, "rolled_back", None, target)
        return UpdateResult("rolled_back", _id(profile_obj), self._strategy(profile_obj), None, target)


__all__ = ["UpdateService", "UpdateResult", "STEAMCMD_ARGV"]
