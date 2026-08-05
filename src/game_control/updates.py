"""Fixed, root-profile-selected update strategies with explicit rollback."""

from __future__ import annotations

import os
import shutil
import stat
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

import httpx

from .errors import SafeError
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
        return UpdateStatus(
            profile_id=profile_obj.id,
            strategy=strategy,
            installed_version=installed,
            available_version=None,
            restart_required=strategy != "manual",
            apply_supported=strategy != "manual",
        )

    def _installed_version(self, profile: Any) -> str | None:
        path = Path(profile.paths.version_file)
        try:
            return path.read_text(encoding="utf-8").strip() or None
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
        return self.runner(list(argv), **kwargs)

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

    def _download(self, profile: Any, destination: Path) -> None:
        if self.downloader is not None:
            try:
                self.downloader(profile, destination)
            except TypeError:
                self.downloader(str(profile.update.download_url), destination)
            return
        response = self.http_client.get(str(profile.update.download_url), timeout=10.0)
        response.raise_for_status()
        destination.write_bytes(response.content)

    def _safe_extract(self, archive_path: Path, staging: Path, expected_relative: str) -> None:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    target = (staging / member.filename).resolve()
                    if staging.resolve() not in target.parents and target != staging.resolve():
                        raise SafeError("update_failed", "release archive is invalid")
                    mode = member.external_attr >> 16
                    kind = stat.S_IFMT(mode)
                    if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise SafeError("update_failed", "release archive is invalid")
                    archive.extract(member, staging)
            return
        try:
            with tarfile.open(archive_path, "r:*") as archive:
                for member in archive.getmembers():
                    target = (staging / member.name).resolve()
                    if staging.resolve() not in target.parents and target != staging.resolve():
                        raise SafeError("update_failed", "release archive is invalid")
                    if not (member.isfile() or member.isdir()):
                        raise SafeError("update_failed", "release archive is invalid")
                archive.extractall(staging)
        except tarfile.TarError:
            # A single executable payload is also accepted by fixed profiles.
            (staging / expected_relative).write_bytes(archive_path.read_bytes())

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
            relative = str(profile.update.executable_relative_path)
            if ".." in Path(relative).parts:
                raise SafeError("update_failed", "release executable is invalid")
            executable = staging / relative
            if not executable.is_file():
                raise SafeError("update_failed", "release executable verification failed")
            if profile.update.version_command:
                command = list(profile.update.version_command) + [str(executable)]
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
            os.replace(staging, destination)
            staging = destination
            temporary_link = install / f".current-{uuid.uuid4().hex}"
            os.symlink(os.path.relpath(destination, install), temporary_link, target_is_directory=True)
            os.replace(temporary_link, current)
            swapped = True
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
        except Exception as exc:
            if swapped:
                self._rollback_link(current, prior_target)
                self._record(profile, "failed", prior_version, new_version)
                self._record(profile, "rolled_back", new_version, prior_version)
            if staging.name.startswith(".release-"):
                shutil.rmtree(staging, ignore_errors=True)
            raise SafeError("update_failed", "release update failed") from exc

    @staticmethod
    def _rollback_link(current: Path, prior_target: str | None) -> None:
        temporary = current.parent / f".rollback-{uuid.uuid4().hex}"
        if prior_target is None:
            current.unlink(missing_ok=True)
        else:
            os.symlink(prior_target, temporary, target_is_directory=True)
            os.replace(temporary, current)

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
