#!/usr/bin/env python3
"""Promote the reviewed Sunlit candidate into its fixed inactive runtime layout."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from game_control.modpack_update import AssemblyError, activate_release


VERSION = "1.1.2-SSV4.1.4"
MANIFEST_SHA256: str | None = "640513b201296f35bdc106f07237acabf0bf2afa7597114e9c8a6ad9f44ac5c3"
STAGING_ROOT = Path("/srv/game-servers/.horizon-update-staging/sunlit-1.1.2-SSV4.1.4")
CANDIDATE = STAGING_ROOT / "candidate-v2"
MANIFEST = STAGING_ROOT / "manifest-v2.json"
RELEASE_ROOT = Path("/opt/game-servers/minecraft-sunlit-cobblemon/releases")
RELEASE = RELEASE_ROOT / VERSION
STATE_ROOT = Path("/srv/game-servers/minecraft-sunlit-cobblemon-state")
ACTIVE_LINK = Path("/srv/game-servers/minecraft-sunlit-cobblemon-current")
SLOT = Path("/run/game-slot/slot.json")
LIBRARIES = Path("/opt/game-servers/minecraft-sunlit-cobblemon/libraries")


class PromotionError(ValueError):
    pass


class PublicationAction(str, Enum):
    STATE = "state"
    RELEASE = "release"
    VERSION_STATE = "version_state"
    METADATA = "metadata"
    ACTIVE_LINK = "active_link"
    ROLLBACK_STATE = "rollback_state"
    ROLLBACK_RELEASE = "rollback_release"
    ROLLBACK_VERSION_STATE = "rollback_version_state"
    ROLLBACK_METADATA = "rollback_metadata"
    ROLLBACK_ACTIVE_LINK = "rollback_active_link"


PublicationGuard = Callable[[PublicationAction], ContextManager[None]]


@dataclass(frozen=True, slots=True)
class PromotionContext:
    """Immutable policy and paths for one promotion operation.

    A promotion is also a public in-process API used by the updater.  Keeping
    its complete path/version identity in a value object prevents concurrent
    calls from borrowing one another's module state.
    """

    version: str
    manifest_sha256: str | None
    staging_root: Path
    candidate: Path
    manifest: Path
    release_root: Path
    release: Path
    state_root: Path
    active_link: Path
    slot: Path
    libraries: Path
    publication_guard: PublicationGuard | None = None


def _validate_version(version: str) -> str:
    if not isinstance(version, str) or not version or "/" in version or version in {".", ".."}:
        raise PromotionError("promotion version is unsafe")
    return version


def _default_context() -> PromotionContext:
    """Snapshot compatibility defaults without mutating module globals."""
    version = _validate_version(VERSION)
    release_root = Path(RELEASE_ROOT)
    candidate = Path(CANDIDATE)
    return PromotionContext(
        version=version,
        manifest_sha256=MANIFEST_SHA256,
        staging_root=Path(STAGING_ROOT),
        candidate=candidate,
        manifest=Path(MANIFEST),
        release_root=release_root,
        release=release_root / version,
        state_root=Path(STATE_ROOT),
        active_link=Path(ACTIVE_LINK),
        slot=Path(SLOT),
        libraries=Path(LIBRARIES),
    )


def promote_candidate(
    *,
    version: str,
    manifest: Path,
    candidate_root: Path,
    manifest_sha256: str | None = None,
    publication_guard: PublicationGuard,
) -> dict:
    """Promote a staged candidate using the package-owned policy.

    The command-line compatibility front door supplies these same values.  The
    updater calls this typed entry point directly, so promotion does not rely
    on an installed checkout helper or a subprocess boundary.
    """
    version = _validate_version(version)
    candidate = Path(candidate_root)
    release_root = Path(RELEASE_ROOT)
    context = PromotionContext(
        version=version,
        manifest_sha256=manifest_sha256,
        staging_root=candidate.parent,
        candidate=candidate,
        manifest=Path(manifest),
        release_root=release_root,
        release=release_root / version,
        state_root=Path(STATE_ROOT),
        active_link=Path(ACTIVE_LINK),
        slot=Path(SLOT),
        libraries=Path(LIBRARIES),
        publication_guard=publication_guard,
    )
    return promote(context)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _regular_json(
    path: Path,
    *,
    maximum: int,
    owners: tuple[tuple[int, int], ...] = ((0, 0),),
) -> dict:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (info.st_uid, info.st_gid) not in owners
        or info.st_mode & 0o022
        or info.st_size > maximum
    ):
        raise PromotionError("promotion metadata is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError("promotion metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise PromotionError("promotion metadata is malformed")
    return value


def _manifest(context: PromotionContext) -> dict:
    document = _regular_json(context.manifest, maximum=8 * 1024 * 1024)
    supplied = document.pop("manifest_sha256", None)
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    calculated = hashlib.sha256(encoded).hexdigest()
    document["manifest_sha256"] = supplied
    if (
        supplied != calculated
        or (context.manifest_sha256 is not None and supplied != context.manifest_sha256)
        or document.get("profile_id") != "minecraft-sunlit-cobblemon"
        or document.get("artifact", {}).get("version") != context.version
    ):
        raise PromotionError("promotion manifest identity mismatch")
    return document


def _inactive(context: PromotionContext) -> None:
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "minecraft-sunlit-cobblemon.service"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.stdout.strip() != "inactive" or context.slot.exists() or context.slot.is_symlink():
        raise PromotionError("Sunlit is not in the required inactive state")


@contextmanager
def _publication(context: PromotionContext, action: PublicationAction) -> Iterator[None]:
    """Enter the caller's atomic ownership fence for one bounded publication.

    The source-only compatibility path has no caller authority and therefore
    fails closed.  The updater must provide a reservation-aware guard
    explicitly; an inactive observation alone is not a publication fence.
    """
    if context.publication_guard is None:
        raise PromotionError("publication guard is required")
    guarded = context.publication_guard(action)
    if not hasattr(guarded, "__enter__") or not hasattr(guarded, "__exit__"):
        raise PromotionError("publication guard is malformed")
    with guarded:
        yield


def _guarded_replace(
    context: PromotionContext,
    action: PublicationAction,
    source: Path,
    destination: Path,
    directory: Path,
) -> None:
    with _publication(context, action):
        os.replace(source, destination)
        _fsync_dir(directory)


def _guarded_activate(
    context: PromotionContext,
    action: PublicationAction,
    release: Path,
    *,
    expected_prior: str | None,
) -> None:
    with _publication(context, action):
        activate_release(
            context.active_link,
            context.release_root,
            release,
            expected_prior=expected_prior,
        )


def _guarded_unlink_active(context: PromotionContext) -> None:
    with _publication(context, PublicationAction.ROLLBACK_ACTIVE_LINK):
        context.active_link.unlink(missing_ok=True)
        _fsync_dir(context.active_link.parent)


def _trusted_directory(
    path: Path,
    *,
    owners: tuple[tuple[int, int], ...] = ((0, 0),),
) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or (info.st_uid, info.st_gid) not in owners
        or info.st_mode & 0o022
    ):
        raise PromotionError("promotion directory is unsafe")
    return info


def _expected_links(document: dict, context: PromotionContext) -> dict[str, str]:
    try:
        policy = document["runtime_policy"]
        state_paths = (
            list(policy["persistent_dirs"])
            + list(policy["persistent_files"])
            + list(policy["mutable_vendor_dirs"])
            + list(policy["empty_mutable_dirs"])
        )
        fixed = dict(policy["fixed_symlinks"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PromotionError("promotion runtime policy is malformed") from exc
    expected: dict[str, str] = {}
    versioned = set(policy["mutable_vendor_dirs"]) | set(policy["empty_mutable_dirs"])
    for relative in state_paths:
        if not isinstance(relative, str) or not relative or "/" in relative or relative in expected:
            raise PromotionError("promotion runtime policy is unsafe")
        target = context.state_root / (Path(".versions") / context.version / relative if relative in versioned else relative)
        expected[relative] = str(target)
    for relative, target in fixed.items():
        if relative in expected or relative != "libraries" or target != str(context.libraries):
            raise PromotionError("promotion fixed-link policy is unsafe")
        expected[relative] = target
    return expected


def _replace_link(path: Path, target: str) -> None:
    temporary = path.parent / f".{path.name}.promote.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise PromotionError("promotion temporary link already exists")
    try:
        os.symlink(target, temporary, target_is_directory=True)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _bind_candidate_links(runtime: Path, document: dict, context: PromotionContext) -> None:
    expected = _expected_links(document, context)
    actual: dict[str, str] = {}
    for path in runtime.rglob("*"):
        if path.is_symlink():
            relative = path.relative_to(runtime).as_posix()
            if "/" in relative:
                raise PromotionError("nested candidate symlink is not approved")
            actual[relative] = os.readlink(path)
    if set(actual) != set(expected):
        raise PromotionError("candidate symlink set is not exact")
    staging_state = str(context.candidate / "state")
    for relative, target in expected.items():
        current = actual[relative]
        if relative == "libraries":
            if current != target:
                raise PromotionError("candidate libraries link changed")
        elif current != target and not current.startswith(staging_state + "/"):
            raise PromotionError("candidate state link changed")
        if current != target:
            _replace_link(runtime / relative, target)


def _write_metadata(
    state: Path,
    document: dict,
    context: PromotionContext,
    *,
    publication_action: PublicationAction | None = None,
    owner: tuple[int, int] | None = None,
) -> None:
    metadata = state / ".horizon"
    if metadata.exists() or metadata.is_symlink():
        if metadata.is_symlink() or not metadata.is_dir():
            raise PromotionError("candidate metadata directory is unsafe")
        if {path.name for path in metadata.iterdir()} - {"manifest.json", "release.json"}:
            raise PromotionError("candidate metadata directory contains unexpected files")
    else:
        metadata.mkdir(mode=0o700)
    manifest_target = metadata / "manifest.json"
    manifest_temporary = metadata / ".manifest.json.promote"
    shutil.copyfile(context.manifest, manifest_temporary, follow_symlinks=False)
    os.chmod(manifest_temporary, 0o600)
    release = {
        "profile_id": "minecraft-sunlit-cobblemon",
        "version": context.version,
        "manifest_sha256": document["manifest_sha256"],
        "archive_sha256": document["artifact"]["archive"]["sha256"],
    }
    release_target = metadata / "release.json"
    release_temporary = metadata / ".release.json.promote"
    release_temporary.write_text(json.dumps(release, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    os.chmod(release_temporary, 0o600)
    if owner is not None:
        for path in (manifest_temporary, release_temporary):
            os.chown(path, owner[0], owner[1], follow_symlinks=False)
            os.chmod(path, 0o640, follow_symlinks=False)
    for path in (manifest_temporary, release_temporary):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    def publish() -> None:
        os.replace(manifest_temporary, manifest_target)
        os.replace(release_temporary, release_target)
        if owner is not None:
            os.chown(metadata, owner[0], owner[1], follow_symlinks=False)
            os.chmod(metadata, 0o750, follow_symlinks=False)
        _fsync_dir(metadata)

    try:
        if publication_action is None:
            publish()
        else:
            with _publication(context, publication_action):
                publish()
    finally:
        manifest_temporary.unlink(missing_ok=True)
        release_temporary.unlink(missing_ok=True)


def _sunlit_ids() -> tuple[int, int]:
    try:
        import pwd
        account = pwd.getpwnam("svc-sunlit")
    except (ImportError, KeyError) as exc:
        raise PromotionError("Sunlit account is unavailable") from exc
    return account.pw_uid, account.pw_gid


def _prepare_state_ownership(root: Path) -> None:
    uid, gid = _sunlit_ids()
    pending = [root]
    paths: list[tuple[Path, os.stat_result]] = []
    while pending:
        current = pending.pop()
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise PromotionError("candidate state contains an unsafe member")
        paths.append((current, info))
        if stat.S_ISDIR(info.st_mode):
            pending.extend(Path(entry.path) for entry in os.scandir(current))
    for path, info in reversed(paths):
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, 0o750 if stat.S_ISDIR(info.st_mode) else 0o640, follow_symlinks=False)


def _normalize_release_permissions(root: Path) -> None:
    """Make an immutable release readable by the unprivileged game user."""
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if info.st_uid != 0 or info.st_gid != 0:
            raise PromotionError("candidate release ownership is unsafe")
        if stat.S_ISDIR(info.st_mode):
            mode = 0o755
        elif stat.S_ISREG(info.st_mode):
            mode = 0o755 if info.st_mode & 0o111 else 0o644
        else:
            raise PromotionError("candidate release ownership is unsafe")
        os.chmod(path, mode, follow_symlinks=False)


def _verify_release_ownership(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise PromotionError("candidate release ownership is unsafe")
        if stat.S_ISDIR(info.st_mode) and info.st_mode & 0o005 != 0o005:
            raise PromotionError("candidate release is not searchable")
        if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o004:
            raise PromotionError("candidate release is not readable")


def _fsync_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise PromotionError("durability root is unsafe")
    os.sync()


def _tree_digest(root: Path, *, include_mode: bool = True) -> str:
    digest = hashlib.sha256()
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.relative_to(root).as_posix() if item != root else ""):
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            kind = b"l"
            payload = os.readlink(path).encode("utf-8")
        elif stat.S_ISDIR(info.st_mode):
            kind = b"d"
            payload = b""
        elif stat.S_ISREG(info.st_mode):
            kind = b"f"
            file_digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            payload = file_digest.digest()
        else:
            raise PromotionError("promotion tree contains an unsafe member")
        digest.update(kind + b"\0" + relative.encode("utf-8") + b"\0")
        if include_mode:
            digest.update(str(stat.S_IMODE(info.st_mode)).encode("ascii") + b"\0")
        digest.update(payload + b"\0")
    return digest.hexdigest()


def _report(context: PromotionContext, document: dict | None = None) -> dict:
    if document is None:
        document = _manifest(context)
    return {
        "active": True,
        "profile_id": "minecraft-sunlit-cobblemon",
        "version": context.version,
        "manifest_sha256": document["manifest_sha256"],
        "release": str(context.release),
        "state": str(context.state_root),
        "active_link": str(context.active_link),
    }


def _publish_report(context: PromotionContext, document: dict | None = None) -> dict:
    report = _report(context, document)
    record = context.candidate / "promotion.json"
    record.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
    os.chmod(record, 0o600)
    with record.open("rb") as stream:
        os.fsync(stream.fileno())
    _fsync_dir(context.candidate)
    return report


def _already_promoted(context: PromotionContext, document: dict, sunlit_owner: tuple[int, int]) -> dict | None:
    expected_target = os.path.relpath(context.release, context.active_link.parent)
    if not context.active_link.is_symlink() or os.readlink(context.active_link) != expected_target:
        return None
    if (context.candidate / "runtime").exists() or (context.candidate / "runtime").is_symlink() or (context.candidate / "state").exists() or (context.candidate / "state").is_symlink():
        raise PromotionError("active promotion retained candidate payload")
    _trusted_directory(context.release)
    _trusted_directory(context.state_root, owners=(sunlit_owner,))
    record_path = context.candidate / "promotion.json"
    if not record_path.exists() and not record_path.is_symlink():
        return _publish_report(context, document)
    record = _regular_json(record_path, maximum=64 * 1024)
    if record != _report(context, document):
        raise PromotionError("active promotion record mismatch")
    return record


def _resume_published_release(context: PromotionContext, runtime: Path, document: dict, sunlit_owner: tuple[int, int]) -> dict | None:
    if not (
        context.release.is_dir()
        and not context.release.is_symlink()
        and context.state_root.is_dir()
        and not context.state_root.is_symlink()
        and runtime.is_dir()
        and not runtime.is_symlink()
        and not (context.candidate / "state").exists()
        and not context.active_link.exists()
        and not context.active_link.is_symlink()
    ):
        return None
    _trusted_directory(context.state_root, owners=(sunlit_owner,))
    _verify_release_ownership(context.release)
    _bind_candidate_links(runtime, document, context)
    if _tree_digest(runtime) != _tree_digest(context.release):
        raise PromotionError("published release does not match the reviewed candidate")
    _guarded_activate(
        context,
        PublicationAction.ACTIVE_LINK,
        context.release,
        expected_prior=None,
    )
    shutil.rmtree(runtime)
    _fsync_dir(context.candidate)
    return _publish_report(context, document)


def _candidate_matches_existing_state(context: PromotionContext, state: Path, document: dict) -> Path:
    """Prove the staged copy did not alter stable state; return only new version state."""
    try:
        policy = document["runtime_policy"]
        stable = tuple(policy["persistent_dirs"]) + tuple(policy["persistent_files"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PromotionError("promotion runtime policy is malformed") from exc
    expected_top = {
        Path(relative).parts[0]
        for relative in stable
        if (context.state_root / relative).exists()
    } | {".versions"}
    actual_top = {path.name for path in state.iterdir()}
    if actual_top != expected_top:
        raise PromotionError("candidate state member set is not exact")
    for relative in stable:
        if not isinstance(relative, str) or not relative or "/" in relative:
            raise PromotionError("promotion runtime policy is unsafe")
        candidate_path = state / relative
        current_path = context.state_root / relative
        if candidate_path.exists() != current_path.exists():
            raise PromotionError("candidate stable state is incomplete")
        if not candidate_path.exists():
            continue
        if candidate_path.is_symlink() or current_path.is_symlink():
            raise PromotionError("candidate stable state is incomplete")
        # Staging intentionally runs as root and may not preserve directory
        # mode bits from the service-owned state tree. It must preserve every
        # member, type, link target, and byte; production state is never
        # replaced by this copy.
        if _tree_digest(candidate_path, include_mode=False) != _tree_digest(current_path, include_mode=False):
            raise PromotionError(f"candidate stable state changed: {relative}")
    candidate_versions = state / ".versions"
    if candidate_versions.is_symlink() or not candidate_versions.is_dir():
        raise PromotionError("candidate version state is unsafe")
    members = list(candidate_versions.iterdir())
    if len(members) != 1 or members[0].name != context.version or members[0].is_symlink() or not members[0].is_dir():
        raise PromotionError("candidate version state is not exact")
    return members[0]


def _metadata_backup(context: PromotionContext, sunlit_owner: tuple[int, int]) -> dict[str, tuple[bytes, int, int, int] | None]:
    metadata = context.state_root / ".horizon"
    if metadata.exists() or metadata.is_symlink():
        _trusted_directory(metadata, owners=((0, 0), sunlit_owner))
    result: dict[str, tuple[bytes, int, int, int] | None] = {}
    for name in ("manifest.json", "release.json"):
        path = metadata / name
        if path.exists() or path.is_symlink():
            _regular_json(path, maximum=8 * 1024 * 1024, owners=((0, 0), sunlit_owner))
            info = path.stat()
            result[name] = (path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
        else:
            result[name] = None
    return result


def _restore_metadata(context: PromotionContext, backup: dict[str, tuple[bytes, int, int, int] | None]) -> None:
    metadata = context.state_root / ".horizon"
    metadata.mkdir(mode=0o700, exist_ok=True)
    prepared: dict[str, Path] = {}
    for name, value in backup.items():
        if value is None:
            continue
        temporary = metadata / f".{name}.rollback"
        temporary.write_bytes(value[0])
        os.chmod(temporary, value[1])
        os.chown(temporary, value[2], value[3])
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        prepared[name] = temporary
    try:
        with _publication(context, PublicationAction.ROLLBACK_METADATA):
            for name, value in backup.items():
                path = metadata / name
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(prepared[name], path)
            _fsync_dir(metadata)
    finally:
        for temporary in prepared.values():
            temporary.unlink(missing_ok=True)


def _promote_upgrade(context: PromotionContext, runtime: Path, state: Path, document: dict, sunlit_owner: tuple[int, int]) -> dict:
    _trusted_directory(context.state_root, owners=(sunlit_owner,))
    if not context.active_link.is_symlink() or context.release.exists() or context.release.is_symlink():
        raise PromotionError("existing deployment is not eligible for an upgrade")
    prior_target = os.readlink(context.active_link)
    prior_release = (context.active_link.parent / prior_target).resolve()
    release_root = context.release_root.resolve()
    if prior_release.parent != release_root or prior_release.is_symlink() or not prior_release.is_dir():
        raise PromotionError("active release target is unsafe")
    version_state = _candidate_matches_existing_state(context, state, document)
    versions_root = context.state_root / ".versions"
    _trusted_directory(versions_root, owners=(sunlit_owner,))
    production_version_state = versions_root / context.version
    if production_version_state.exists() or production_version_state.is_symlink():
        raise PromotionError("production version state already exists")
    if version_state.stat().st_dev != versions_root.stat().st_dev:
        raise PromotionError("version state promotion requires one filesystem")
    _bind_candidate_links(runtime, document, context)
    _normalize_release_permissions(runtime)
    _verify_release_ownership(runtime)
    metadata_backup = _metadata_backup(context, sunlit_owner)
    release_stage = context.release_root / f".{context.version}.promote.{os.getpid()}"
    release_published = False
    version_state_moved = False
    metadata_attempted = False
    activated = False
    if release_stage.exists() or release_stage.is_symlink():
        raise PromotionError("release staging path already exists")
    try:
        shutil.copytree(runtime, release_stage, symlinks=True)
        _verify_release_ownership(release_stage)
        _fsync_tree(release_stage)
        _prepare_state_ownership(version_state)
        _guarded_replace(
            context,
            PublicationAction.RELEASE,
            release_stage,
            context.release,
            context.release_root,
        )
        release_published = True
        _guarded_replace(
            context,
            PublicationAction.VERSION_STATE,
            version_state,
            production_version_state,
            versions_root,
        )
        version_state_moved = True
        metadata_attempted = True
        _write_metadata(
            context.state_root,
            document,
            context,
            publication_action=PublicationAction.METADATA,
            owner=sunlit_owner,
        )
        _guarded_activate(
            context,
            PublicationAction.ACTIVE_LINK,
            context.release,
            expected_prior=prior_target,
        )
        activated = True
    except BaseException:
        if activated and context.active_link.is_symlink():
            _guarded_activate(
                context,
                PublicationAction.ROLLBACK_ACTIVE_LINK,
                prior_release,
                expected_prior=os.readlink(context.active_link),
            )
        if metadata_attempted:
            _restore_metadata(context, metadata_backup)
        if version_state_moved and production_version_state.exists() and not version_state.exists():
            _guarded_replace(
                context,
                PublicationAction.ROLLBACK_VERSION_STATE,
                production_version_state,
                version_state,
                versions_root,
            )
        if release_published and context.release.exists():
            rollback_release = context.release_root / f".{context.version}.rollback.{os.getpid()}"
            if rollback_release.exists() or rollback_release.is_symlink():
                raise PromotionError("release rollback path already exists")
            _guarded_replace(
                context,
                PublicationAction.ROLLBACK_RELEASE,
                context.release,
                rollback_release,
                context.release_root,
            )
            shutil.rmtree(rollback_release)
        if release_stage.exists():
            shutil.rmtree(release_stage)
        raise
    shutil.rmtree(runtime)
    shutil.rmtree(state)
    _fsync_dir(context.candidate)
    return _publish_report(context, document)


def _promote(context: PromotionContext) -> dict:
    if os.geteuid() != 0:
        raise PromotionError("promotion requires root")
    _inactive(context)
    document = _manifest(context)
    _trusted_directory(context.candidate)
    candidate = _regular_json(context.candidate / "candidate.json", maximum=64 * 1024)
    if candidate.get("active") is not False or candidate.get("version") != context.version or candidate.get("manifest_sha256") != document["manifest_sha256"]:
        raise PromotionError("candidate record identity mismatch")
    _trusted_directory(context.release_root)
    _trusted_directory(context.state_root.parent)
    _trusted_directory(context.active_link.parent)
    sunlit_owner = _sunlit_ids()
    completed = _already_promoted(context, document, sunlit_owner)
    if completed is not None:
        return completed
    runtime = context.candidate / "runtime"
    state = context.candidate / "state"
    _trusted_directory(runtime)
    _trusted_directory(state, owners=((0, 0), sunlit_owner))
    resumed = _resume_published_release(context, runtime, document, sunlit_owner)
    if resumed is not None:
        return resumed
    if context.state_root.exists() and context.active_link.is_symlink():
        return _promote_upgrade(context, runtime, state, document, sunlit_owner)
    if any(path.exists() or path.is_symlink() for path in (context.release, context.state_root, context.active_link)):
        raise PromotionError("production candidate destination already exists")
    if state.stat().st_dev != context.state_root.parent.stat().st_dev:
        raise PromotionError("state promotion requires one filesystem")
    _bind_candidate_links(runtime, document, context)
    _normalize_release_permissions(runtime)
    _write_metadata(state, document, context)
    _prepare_state_ownership(state)
    _verify_release_ownership(runtime)
    state_moved = False
    release_published = False
    release_stage = context.release_root / f".{context.version}.promote.{os.getpid()}"
    if release_stage.exists() or release_stage.is_symlink():
        raise PromotionError("release staging path already exists")
    try:
        # Build, verify, and durably flush the potentially large release while
        # the caller's short operation-lock publication fence remains free.
        shutil.copytree(runtime, release_stage, symlinks=True)
        _verify_release_ownership(release_stage)
        _fsync_tree(release_stage)
        _guarded_replace(
            context,
            PublicationAction.STATE,
            state,
            context.state_root,
            context.state_root.parent,
        )
        state_moved = True
        _guarded_replace(
            context,
            PublicationAction.RELEASE,
            release_stage,
            context.release,
            context.release_root,
        )
        release_published = True
        _guarded_activate(
            context,
            PublicationAction.ACTIVE_LINK,
            context.release,
            expected_prior=None,
        )
    except BaseException:
        if context.active_link.is_symlink():
            _guarded_unlink_active(context)
        if release_published and context.release.exists():
            rollback_release = context.release_root / f".{context.version}.rollback.{os.getpid()}"
            if rollback_release.exists() or rollback_release.is_symlink():
                raise PromotionError("release rollback path already exists")
            _guarded_replace(
                context,
                PublicationAction.ROLLBACK_RELEASE,
                context.release,
                rollback_release,
                context.release_root,
            )
            shutil.rmtree(rollback_release)
        if release_stage.exists():
            shutil.rmtree(release_stage)
        if state_moved and context.state_root.exists() and not state.exists():
            _guarded_replace(
                context,
                PublicationAction.ROLLBACK_STATE,
                context.state_root,
                state,
                state.parent,
            )
        raise
    shutil.rmtree(runtime)
    _fsync_dir(context.candidate)
    return _publish_report(context, document)


def promote(context: PromotionContext | None = None) -> dict:
    """Promote one candidate only with explicit publication authority."""
    if context is None or context.publication_guard is None:
        raise PromotionError("publication guard is required")
    return _promote(context)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--candidate-root", type=Path, default=CANDIDATE)
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args(argv)
    # These source-only compatibility names are historical fixed-policy front
    # doors.  The normal updater uses promote_candidate() and may supply its
    # reviewed staging root; this CLI must never turn arbitrary caller paths
    # into a promotion authority.
    if (
        args.version != VERSION
        or args.manifest != Path(MANIFEST)
        or args.candidate_root != Path(CANDIDATE)
        or (args.manifest_sha256 is not None and args.manifest_sha256 != MANIFEST_SHA256)
    ):
        return 2
    print("error: historical promotion front door is unsupported", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
