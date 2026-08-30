#!/usr/bin/env python3
"""Discover, stage, back up, and atomically promote official Sunlit releases."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .sunlit_manifest import ManifestError, atomic_write, make_manifest
from .modpack_update import AssemblyError, MAX_TOTAL_SIZE
from .slot import OperationLock, ReservationStore
from .sunlit_promote import PromotionError, promote_candidate
from .sunlit_stage import StageError, stage

PROFILE = "minecraft-sunlit-cobblemon"
PROJECT_ID = "1495800"
API_ROOT = f"https://www.curseforge.com/api/v1/mods/{PROJECT_ID}"
STAGING_ROOT = Path("/srv/game-servers/.horizon-update-staging")
STATE_ROOT = Path("/srv/game-servers/minecraft-sunlit-cobblemon-state")
RELEASE_ROOT = Path("/opt/game-servers/minecraft-sunlit-cobblemon/releases")
ACTIVE_LINK = Path("/srv/game-servers/minecraft-sunlit-cobblemon-current")
SLOT = Path("/run/game-slot/slot.json")
DATABASE = Path("/var/lib/game-control/state.db")
OVERLAY = Path("/srv/game-servers/minecraft-sunlit-cobblemon/mods/Prometheus-Exporter-1.20.1-forge-1.2.1.jar")
OVERLAY_DESTINATION = "mods/Prometheus-Exporter-1.20.1-forge-1.2.1.jar"
# The backup RPC is an explicit protected boundary.  Manifest, staging, and
# promotion are package-owned and must not cross an absent helper boundary.
RPC_HELPER = Path("/usr/local/libexec/horizon-sunlit-update-rpc")
PYTHON = Path("/opt/game-control/.venv/bin/python")
VERSION_RE = re.compile(r"^SERVER-PACK-Society-Sunlit-Cobblemon-([A-Za-z0-9][A-Za-z0-9._-]{0,126})\.zip$")
MAX_JSON = 4 * 1024 * 1024
MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
SPACE_MARGIN = 2 * 1024 * 1024 * 1024
MAX_OVERLAY_SIZE = 256 * 1024 * 1024
OPERATION_LOCK = Path("/run/game-control/operation.lock")
RESERVATION_FILE = Path("/run/game-control/reservation.json")
RESERVATION_TTL = 30.0
RESERVATION_RENEW_INTERVAL = 10.0


class UpdateError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Horizon-Sunlit-Updater/1"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            if response.status != 200 or response.geturl() != url:
                raise UpdateError("upstream metadata response is not exact")
            raw = response.read(MAX_JSON + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError("upstream metadata is unavailable") from exc
    if len(raw) > MAX_JSON:
        raise UpdateError("upstream metadata exceeds bound")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("upstream metadata is malformed") from exc
    if not isinstance(value, dict):
        raise UpdateError("upstream metadata is malformed")
    return value


def discover() -> dict:
    files = _fetch_json(f"{API_ROOT}/files").get("data")
    if not isinstance(files, list):
        raise UpdateError("upstream file list is malformed")
    eligible = []
    for item in files:
        if not isinstance(item, dict):
            continue
        versions = item.get("gameVersions")
        if (
            item.get("releaseType") == 1
            and item.get("fileStatus") in (None, 4)
            and item.get("hasServerPack") is True
            and isinstance(versions, list)
            and "1.20.1" in versions
            and "Forge" in versions
            and isinstance(item.get("id"), int)
        ):
            eligible.append(item)
    if not eligible:
        raise UpdateError("no eligible official Sunlit release was found")
    main = max(eligible, key=lambda item: item["id"])
    additional = _fetch_json(f"{API_ROOT}/files/{main['id']}/additional-files").get("data")
    if not isinstance(additional, list) or len(additional) != 1 or not isinstance(additional[0], dict):
        raise UpdateError("official server-pack relationship is not exact")
    server = additional[0]
    name = server.get("fileName")
    match = VERSION_RE.fullmatch(name) if isinstance(name, str) else None
    file_id = server.get("id")
    size = server.get("fileLength")
    if match is None or not isinstance(file_id, int) or file_id <= 0 or not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
        raise UpdateError("official server-pack identity is malformed")
    return {
        "version": match.group(1),
        "main_file_id": str(main["id"]),
        "file_id": str(file_id),
        "file_name": name,
        "size": size,
        "url": f"{API_ROOT}/files/{file_id}/download",
    }


def _digest(path: Path) -> tuple[int, str]:
    total = 0
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            value.update(chunk)
    return total, value.hexdigest()


def _load_json(path: Path, maximum: int = MAX_JSON) -> dict:
    try:
        info = path.lstat()
    except OSError as exc:
        raise UpdateError("local update metadata is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise UpdateError("local update metadata is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("local update metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise UpdateError("local update metadata is malformed")
    return value


def _installed_version() -> str | None:
    record = STATE_ROOT / ".horizon/release.json"
    try:
        if not record.exists():
            return None
        value = _load_json(record, 64 * 1024).get("version")
        if not isinstance(value, str) or not value:
            return None
        # Stable metadata is not the commit record.  The active link is the
        # publication point; never report a version that was only copied into
        # state before activation completed.
        if not ACTIVE_LINK.is_symlink():
            return None
        release_root = RELEASE_ROOT.resolve()
        release = release_root / value
        target = (ACTIVE_LINK.parent / os.readlink(ACTIVE_LINK)).resolve(strict=True)
        if target != release.resolve() or target.parent != release_root:
            return None
        info = release.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
        return value
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError("installed Sunlit version cannot be verified") from exc


def _inactive() -> bool:
    """Prove that no lifecycle owner or service is active.

    This function is also used as the reservation precondition.  It must not
    leak implementation errors into the systemd timer: unreadable service,
    slot, or state-database observations are bounded updater failures.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "minecraft-sunlit-cobblemon.service"],
            check=False, capture_output=True, text=True,
        )
        state = result.stdout.strip()
        try:
            slot_claimed = SLOT.lstat()
        except FileNotFoundError:
            slot_claimed = None
        if state != "inactive" or slot_claimed is not None:
            return False
        with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            active_session = db.execute(
                "SELECT 1 FROM player_sessions WHERE profile_id=? AND ended_at IS NULL LIMIT 1", (PROFILE,)
            ).fetchone()
            active_job = db.execute(
                "SELECT 1 FROM jobs WHERE profile_id=? AND state IN ('accepted','running') LIMIT 1", (PROFILE,)
            ).fetchone()
        return active_session is None and active_job is None
    except (OSError, sqlite3.Error, AttributeError, TypeError) as exc:
        raise UpdateError("Sunlit inactive state cannot be verified") from exc


def _state_generation() -> int:
    """Read the root generation through the same query-only state boundary."""
    try:
        with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute("PRAGMA application_id").fetchone()
        value = row[0] if row is not None and len(row) == 1 else None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("malformed root generation")
        return value
    except (OSError, sqlite3.Error, TypeError, ValueError, IndexError) as exc:
        raise UpdateError("Sunlit root generation cannot be verified") from exc


def _tree_bytes(path: Path, *, skip_symlinks: bool = False) -> int:
    """Return regular-file bytes without following links or hiding races."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise UpdateError("persistent state size cannot be proven") from exc
    if stat.S_ISLNK(info.st_mode):
        if skip_symlinks:
            return 0
        raise UpdateError("persistent state root is a symlink")
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise UpdateError("persistent state contains an unsafe member")
    total = 0
    try:
        entries = list(os.scandir(path))
    except OSError as exc:
        raise UpdateError("persistent state size cannot be proven") from exc
    for entry in entries:
        total += _tree_bytes(Path(entry.path), skip_symlinks=skip_symlinks)
    return total


class _UpdateLease:
    """Keep the shared lifecycle reservation alive for long updater stages."""

    def __init__(
        self,
        store: ReservationStore,
        operation_id: str,
        state_generation: int = 0,
        *,
        controller_pid: int,
        controller_start_ticks: int,
    ):
        self.store = store
        self.operation_id = operation_id
        self.state_generation = state_generation
        self.controller_pid = controller_pid
        self.controller_start_ticks = controller_start_ticks
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._released = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._renew, name="horizon-sunlit-update-lease", daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(RESERVATION_RENEW_INTERVAL):
            try:
                self.store.renew_if_owned(
                    PROFILE, self.operation_id, RESERVATION_TTL,
                    state_generation=self.state_generation,
                    operation_kind="update",
                    controller_pid=self.controller_pid,
                    controller_start_ticks=self.controller_start_ticks,
                )
            except BaseException:
                self._lost.set()
                return

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise UpdateError("Sunlit update reservation was lost")
        try:
            owned = self.store.owns_live(
                PROFILE, self.operation_id, self.state_generation, operation_kind="update",
                controller_pid=self.controller_pid,
                controller_start_ticks=self.controller_start_ticks,
            )
        except (OSError, ValueError, PermissionError, BlockingIOError) as exc:
            raise UpdateError("Sunlit update reservation cannot be verified") from exc
        if not owned:
            self._lost.set()
            raise UpdateError("Sunlit update reservation was lost")

    def assert_owned_locked(self) -> None:
        if self._lost.is_set():
            raise UpdateError("Sunlit update reservation was lost")
        try:
            owned = self.store.owns_live_locked(
                PROFILE, self.operation_id, self.state_generation,
                controller_pid=self.controller_pid,
                controller_start_ticks=self.controller_start_ticks,
            )
        except (OSError, ValueError, PermissionError) as exc:
            raise UpdateError("Sunlit update reservation cannot be verified") from exc
        if not owned:
            self._lost.set()
            raise UpdateError("Sunlit update reservation was lost")

    @contextmanager
    def publication_guard(self, action):
        """Fence one bounded promotion rename with operation.lock ownership."""
        action_value = getattr(action, "value", action)
        if action_value not in {
            "state", "release", "version_state", "metadata", "active_link",
            "rollback_state", "rollback_release", "rollback_version_state",
            "rollback_metadata", "rollback_active_link",
        }:
            raise UpdateError("Sunlit publication action is not approved")
        try:
            with OperationLock(OPERATION_LOCK):
                self.assert_owned_locked()
                if not _inactive():
                    raise UpdateError("Sunlit became active before publication")
                yield
        except UpdateError:
            raise
        except (OSError, ValueError) as exc:
            raise UpdateError("Sunlit publication lock is unavailable") from exc

    def close(self) -> None:
        self.pause()
        if self._released:
            return
        try:
            released = self.store.release_if_owned(
                PROFILE, self.operation_id, self.state_generation, operation_kind="update",
                controller_pid=self.controller_pid,
                controller_start_ticks=self.controller_start_ticks,
            )
        except (OSError, ValueError, PermissionError, BlockingIOError) as exc:
            raise UpdateError("Sunlit update reservation could not be released") from exc
        if not released and not self._lost.is_set():
            raise UpdateError("Sunlit update reservation ownership changed")

    def release_locked(self) -> None:
        if self._released:
            return
        try:
            released = self.store.release_if_owned_locked(
                PROFILE, self.operation_id, self.state_generation,
                operation_kind="update",
                controller_pid=self.controller_pid,
                controller_start_ticks=self.controller_start_ticks,
            )
        except (OSError, ValueError, PermissionError) as exc:
            raise UpdateError("Sunlit update reservation could not be released") from exc
        if not released:
            raise UpdateError("Sunlit update reservation ownership changed")
        self._released = True

    def pause(self) -> None:
        """Stop renewals before taking the final exclusive operation lock."""
        self._stop.set()
        if self._thread is not None:
            # Renewal must be fully drained before ownership is released.  A
            # timed join could leave a worker behind that rewrites a lease
            # after close() has returned.
            self._thread.join()
            self._thread = None


def _space_available(release: dict, *, manifest: dict | None = None) -> bool:
    try:
        archive_size = release["size"]
        if (
            isinstance(archive_size, bool)
            or not isinstance(archive_size, int)
            or not 0 < archive_size <= MAX_ARCHIVE
        ):
            raise ValueError("invalid archive size")
        state_bytes = _tree_bytes(STATE_ROOT)
        if manifest is None:
            expanded_size = MAX_TOTAL_SIZE
        else:
            archive = manifest.get("archive")
            expanded_size = archive.get("total_uncompressed_size") if isinstance(archive, dict) else None
            if (
                isinstance(expanded_size, bool)
                or not isinstance(expanded_size, int)
                or not 0 < expanded_size <= MAX_TOTAL_SIZE
            ):
                raise UpdateError("archive expansion bound is unavailable")
        overlay_info = OVERLAY.lstat()
        if (
            not stat.S_ISREG(overlay_info.st_mode)
            or overlay_info.st_nlink != 1
            or overlay_info.st_uid != 0
            or overlay_info.st_mode & 0o022
            or not 0 < overlay_info.st_size <= MAX_OVERLAY_SIZE
        ):
            raise UpdateError("Sunlit overlay size cannot be proven")
        try:
            release_root_info = RELEASE_ROOT.lstat()
        except FileNotFoundError:
            release_root_info = None
        if release_root_info is not None and stat.S_ISLNK(release_root_info.st_mode):
            raise UpdateError("release root is a symlink")
        # Existing releases/staging already consume disk_usage.free and are
        # therefore inspected for safety but not charged a second time.
        _tree_bytes(RELEASE_ROOT, skip_symlinks=True)
        _tree_bytes(STAGING_ROOT, skip_symlinks=True)
        # During assembly the compressed archive, expanded vendor tree, and
        # expanded runtime coexist. Reserve the declared worst-case archive
        # expansion rather than the attacker-controlled compressed size alone.
        # A compressed archive, expanded vendor tree, expanded runtime,
        # versioned persistent copies, and two overlay copies coexist. The
        # active release and existing staging remain in disk_usage.free's
        # occupied baseline and are intentionally not double-counted.
        required = archive_size + 3 * expanded_size + 3 * state_bytes + 2 * overlay_info.st_size + SPACE_MARGIN
        return shutil.disk_usage(STAGING_ROOT).free >= required
    except UpdateError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise UpdateError("free space for Sunlit update cannot be proven") from exc


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"update helper failed: {Path(argv[0]).name}") from exc


def _download(release: dict, target: Path) -> str:
    temporary = target.parent / f".{target.name}.download"
    if temporary.exists() or temporary.is_symlink():
        raise UpdateError("partial archive already exists")
    request = urllib.request.Request(release["url"], headers={"Accept": "application/octet-stream", "User-Agent": "Horizon-Sunlit-Updater/1"})
    total = 0
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urllib.parse.urlsplit(response.geturl())
            source = urllib.parse.urlsplit(release["url"])
            trusted_final = (
                final.scheme == "https"
                and not final.username
                and not final.password
                and not final.fragment
                and (
                    response.geturl() == release["url"]
                    or (final.hostname is not None and final.hostname.endswith(".forgecdn.net"))
                )
                and source.scheme == "https"
            )
            if response.status != 200 or not trusted_final:
                raise UpdateError("server-pack download response is not exact")
            with temporary.open("xb") as output:
                os.chmod(temporary, 0o600)
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > release["size"] or total > MAX_ARCHIVE:
                        raise UpdateError("server-pack download exceeds bound")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total != release["size"]:
            raise UpdateError("server-pack download size mismatch")
        os.replace(temporary, target)
        return digest.hexdigest()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _staging_root(release: dict) -> Path:
    version = release.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(
        f"SERVER-PACK-Society-Sunlit-Cobblemon-{version}.zip"
    ):
        raise UpdateError("staging release identity is unsafe")
    try:
        root_info = STAGING_ROOT.lstat()
    except FileNotFoundError:
        try:
            STAGING_ROOT.mkdir(parents=True, mode=0o700)
            root_info = STAGING_ROOT.lstat()
        except OSError as exc:
            raise UpdateError("update staging root is unavailable") from exc
    except OSError as exc:
        raise UpdateError("update staging root is unavailable") from exc
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != 0
        or root_info.st_gid != 0
        or root_info.st_mode & 0o022
    ):
        raise UpdateError("update staging root is unsafe")
    root = STAGING_ROOT / f"sunlit-{version}"
    try:
        info = root.lstat()
    except FileNotFoundError:
        try:
            root.mkdir(mode=0o700)
            info = root.lstat()
        except OSError as exc:
            raise UpdateError("update staging root is unavailable") from exc
    except OSError as exc:
        raise UpdateError("update staging root is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or info.st_mode & 0o022
    ):
        raise UpdateError("existing staging root is unsafe")
    return root


def _staged_operation_id(root: Path) -> str:
    """Load one durable UUID for all retries of a staged release."""
    path = root / "update-operation-id"
    try:
        info = path.lstat()
    except FileNotFoundError:
        # A populated pre-Wave-5 staging directory cannot be safely adopted:
        # creating a new owner would permit a restarted updater to take over
        # work whose original reservation may still be live.
        try:
            populated = any(root.iterdir())
        except OSError as exc:
            raise UpdateError("staged update identity is unavailable") from exc
        if populated:
            raise UpdateError("staged update identity is missing")
        operation_id = str(uuid.uuid4())
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(operation_id + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, UnicodeError) as exc:
            path.unlink(missing_ok=True)
            raise UpdateError("staged update identity is unavailable") from exc
        return operation_id
    except OSError as exc:
        raise UpdateError("staged update identity is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 128
    ):
        raise UpdateError("staged update identity is unsafe")
    try:
        value = path.read_text(encoding="ascii").strip()
        parsed = uuid.UUID(value)
    except (OSError, UnicodeError, ValueError) as exc:
        raise UpdateError("staged update identity is malformed") from exc
    if str(parsed) != value:
        raise UpdateError("staged update identity is malformed")
    return value


def _stage(release: dict) -> tuple[Path, dict]:
    root = _staging_root(release)
    _staged_operation_id(root)
    archive = root / "server-pack.zip"
    manifest_path = root / "manifest.json"
    try:
        if manifest_path.exists() or manifest_path.is_symlink():
            manifest = _load_json(manifest_path)
            artifact = manifest.get("artifact", {})
            if (
                artifact.get("project_id") != PROJECT_ID
                or artifact.get("file_id") != release["file_id"]
                or artifact.get("version") != release["version"]
                or artifact.get("archive", {}).get("size") != release["size"]
            ):
                raise UpdateError("existing staging identity does not match upstream")
        else:
            if archive.is_symlink():
                raise UpdateError("existing server-pack archive is unsafe")
            if archive.exists():
                size, sha256 = _digest(archive)
                if size != release["size"]:
                    raise UpdateError("existing server-pack archive is unsafe")
            else:
                sha256 = _download(release, archive)
            _overlay_size, overlay_sha = _digest(OVERLAY)
            try:
                manifest = make_manifest(argparse.Namespace(
                    archive=archive,
                    version=release["version"],
                    project_id=PROJECT_ID,
                    file_id=release["file_id"],
                    url=release["url"],
                    archive_size=release["size"],
                    archive_sha256=sha256,
                    overlay_source=OVERLAY,
                    overlay_destination=OVERLAY_DESTINATION,
                    overlay_sha256=overlay_sha,
                ))
                atomic_write(manifest_path, (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
            except (ManifestError, OSError) as exc:
                raise UpdateError("manifest generation failed") from exc
        if not _space_available(release, manifest=manifest):
            raise UpdateError("insufficient free space for the measured Sunlit archive")
        candidate_path = root / "candidate/candidate.json"
        if candidate_path.exists() or candidate_path.is_symlink():
            candidate = _load_json(candidate_path, 64 * 1024)
            if candidate.get("version") != release["version"] or candidate.get("manifest_sha256") != manifest.get("manifest_sha256"):
                raise UpdateError("existing staged candidate identity mismatch")
        else:
            if not archive.is_file() or archive.is_symlink():
                raise UpdateError("staging archive is unavailable")
            if _digest(archive) != (
                manifest["artifact"]["archive"]["size"],
                manifest["artifact"]["archive"]["sha256"],
            ):
                raise UpdateError("staging archive changed after manifest creation")
            try:
                stage(argparse.Namespace(
                    manifest=manifest_path,
                    archive=archive,
                    prior_runtime=STATE_ROOT,
                    candidate_root=root / "candidate",
                ))
            except (AssemblyError, StageError, OSError, KeyError, TypeError, ValueError) as exc:
                raise UpdateError("candidate staging failed") from exc
        return root, manifest
    except BaseException:
        # Preserve a completed manifest/archive for forensic inspection, but
        # never leave a partial candidate that a later run could promote.
        candidate = root / "candidate"
        if candidate.exists() and not (candidate / "candidate.json").is_file():
            shutil.rmtree(candidate, ignore_errors=True)
        raise


def _request_backup(root: Path) -> str:
    request_file = root / "backup-request-id"
    try:
        if request_file.exists():
            request_id = request_file.read_text(encoding="ascii").strip()
            uuid.UUID(request_id)
        else:
            request_id = str(uuid.uuid4())
            fd = os.open(request_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(request_id + "\n")
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, UnicodeError, ValueError) as exc:
        raise UpdateError("backup request state is unavailable") from exc
    result = _run([
        "/usr/sbin/runuser", "-u", "gamecontrol", "--", "/usr/bin/env", "PYTHONPATH=/opt/game-control/src",
        str(PYTHON), str(RPC_HELPER), "backup", "--request-id", request_id,
    ], timeout=7_300)
    try:
        backup_id = json.loads(result.stdout)["job_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError("backup response is malformed") from exc
    try:
        with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT b.verified,b.protected,p.upload_state,p.remote_verified,p.comparison_state "
                "FROM backups b JOIN backup_protections p ON p.backup_id=b.id "
                "WHERE b.id=? AND b.profile_id=? AND p.profile_id=? AND p.destination_id='horizon-b2' AND p.backup_class='application'",
                (backup_id, PROFILE, PROFILE),
            ).fetchone()
    except sqlite3.Error as exc:
        raise UpdateError("backup protection state is unavailable") from exc
    if row != (1, 1, "succeeded", 1, "verified"):
        raise UpdateError("pre-update backup is not fully protected")
    return str(backup_id)


def _record(prior: str | None, new: str) -> None:
    try:
        with sqlite3.connect(DATABASE, timeout=5) as db:
            db.execute(
                "INSERT INTO updates(id,profile_id,created_at,strategy,prior_version,new_version,state) "
                "VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),'curated_modpack',?,?,?)",
                (uuid.uuid4().hex, PROFILE, prior, new, "succeeded"),
            )
            db.commit()
    except sqlite3.Error as exc:
        raise UpdateError("update history state is unavailable") from exc


def _reserve_update(operation_id: str) -> _UpdateLease | None:
    """Atomically reserve the profile only while its stopped state is proven."""
    try:
        store = ReservationStore(
            operation_path=OPERATION_LOCK,
            reservation_path=RESERVATION_FILE,
        )
        reservation = store.reserve_if_available(
            PROFILE,
            operation_id,
            RESERVATION_TTL,
            state_generation=0,
            availability_check=_inactive,
            operation_kind="update",
            generation_provider=_state_generation,
        )
    except BlockingIOError:
        return None
    except UpdateError:
        raise
    except (OSError, ValueError, PermissionError, sqlite3.Error) as exc:
        raise UpdateError("Sunlit update reservation is unavailable") from exc
    lease = _UpdateLease(
        store,
        operation_id,
        reservation.state_generation,
        controller_pid=reservation.controller_pid,
        controller_start_ticks=reservation.controller_start_ticks,
    )
    lease.start()
    return lease


def run(*, check_only: bool) -> dict:
    release = discover()
    installed = _installed_version()
    if installed == release["version"]:
        return {"state": "current", "installed": installed, "available": None}
    if check_only:
        return {"state": "available", "installed": installed, "available": release["version"], "file_id": release["file_id"]}
    if os.geteuid() != 0:
        raise UpdateError("automatic update requires root")
    if not _inactive():
        return {"state": "deferred", "installed": installed, "available": release["version"]}
    # Establish or validate the fixed staging identity before taking the
    # reservation.  This is metadata-only and lets retries reuse one UUID;
    # the reservation precondition below still closes a lifecycle race before
    # any archive or candidate mutation begins.
    staging_root = _staging_root(release)
    operation_id = _staged_operation_id(staging_root)
    lease = _reserve_update(operation_id)
    if lease is None:
        return {"state": "deferred", "installed": installed, "available": release["version"]}
    primary_error: BaseException | None = None
    try:
        lease.assert_owned()
        if not _space_available(release):
            raise UpdateError("insufficient free space for a staged update and rollback margin")
        try:
            root, manifest = _stage(release)
        except UpdateError:
            raise
        except (AssemblyError, OSError, KeyError, TypeError, ValueError) as exc:
            raise UpdateError("candidate staging failed") from exc
        lease.assert_owned()
        backup_id = _request_backup(root)
        lease.assert_owned()
        try:
            promotion = promote_candidate(
                version=release["version"],
                manifest=root / "manifest.json",
                candidate_root=root / "candidate",
                manifest_sha256=manifest["manifest_sha256"],
                publication_guard=lease.publication_guard,
            )
        except UpdateError:
            raise
        except (AssemblyError, OSError, PromotionError, KeyError, TypeError, ValueError) as exc:
            raise UpdateError("candidate promotion failed") from exc
        lease.assert_owned()
        with lease.publication_guard("metadata"):
            _record(installed, release["version"])
            lease.release_locked()
        (root / "server-pack.zip").unlink(missing_ok=True)
        return {"state": "promoted", "installed": release["version"], "available": None, "backup_id": backup_id}
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            lease.close()
        except BaseException:
            if primary_error is None:
                raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    lock_path = Path("/run/lock/horizon-sunlit-update.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "deferred", "reason": "update already running"}, sort_keys=True))
            return 0
        try:
            print(json.dumps(run(check_only=args.check), sort_keys=True))
        except UpdateError as exc:
            print(f"error: {exc}", file=os.sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
