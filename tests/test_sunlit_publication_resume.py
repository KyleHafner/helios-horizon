from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from game_control import sunlit_promote as promote
from game_control import sunlit_update as update


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def _document(version: str, libraries: Path) -> tuple[dict, str]:
    value = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": version, "archive": {"sha256": version[0] * 64}},
        "runtime_policy": {
            "persistent_dirs": ["world"],
            "persistent_files": ["ops.json", "optional.properties"],
            "required_paths": ["world", "ops.json"],
            "mutable_vendor_dirs": ["config"],
            "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
        },
    }
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    value["manifest_sha256"] = digest
    return value, digest


def _post_action_guard(refused: promote.PublicationAction):
    state = {"refuse": True}

    @contextmanager
    def guard(action: promote.PublicationAction) -> Iterator[None]:
        yield
        if state["refuse"] and action is refused:
            state["refuse"] = False
            raise OSError("synthetic post-action ownership loss")

    return guard


def _fresh_context(
    tmp_path: Path, monkeypatch, refused: promote.PublicationAction,
) -> promote.PromotionContext:
    candidate = tmp_path / "stage/candidate"
    runtime = candidate / "runtime"
    state = candidate / "state"
    releases = tmp_path / "releases"
    state_root = tmp_path / "stable/state"
    active = tmp_path / "stable/current"
    libraries = tmp_path / "libraries"
    for path in (
        runtime / "mods", state / "world", state / ".versions/v1/config",
        state / ".versions/v1/logs", releases, active.parent, libraries,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods/new.jar").write_bytes(b"new")
    (state / "world/level.dat").write_bytes(b"world")
    (state / "ops.json").write_bytes(b"ops")
    for name, target in {
        "world": state / "world",
        "ops.json": state / "ops.json",
        "optional.properties": state / "optional.properties",
        "config": state / ".versions/v1/config",
        "logs": state / ".versions/v1/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document, digest = _document("v1", libraries)
    manifest = tmp_path / "stage/manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {
        "active": False, "version": "v1", "manifest_sha256": digest,
    })
    monkeypatch.setattr(promote, "_inactive", lambda _context: None)
    monkeypatch.setattr(promote, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))
    return promote.PromotionContext(
        "v1", digest, candidate.parent, candidate, manifest, releases,
        releases / "v1", state_root, active, tmp_path / "slot", libraries,
        _post_action_guard(refused),
    )


def _upgrade_context(
    tmp_path: Path, monkeypatch, refused: promote.PublicationAction,
) -> tuple[promote.PromotionContext, Path]:
    candidate = tmp_path / "stage/candidate"
    runtime = candidate / "runtime"
    state = candidate / "state"
    releases = tmp_path / "releases"
    old_release = releases / "v1"
    state_root = tmp_path / "stable/state"
    active = tmp_path / "stable/current"
    libraries = tmp_path / "libraries"
    for path in (
        runtime / "mods", state / "world", state / ".versions/v2/config",
        state / ".versions/v2/logs", old_release, state_root / "world",
        state_root / ".versions/v1/config", state_root / ".horizon", libraries,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods/new.jar").write_bytes(b"new")
    (state / "world/level.dat").write_bytes(b"world")
    (state / "ops.json").write_bytes(b"ops")
    (state_root / "world/level.dat").write_bytes(b"world")
    (state_root / "ops.json").write_bytes(b"ops")
    active.symlink_to(
        os.path.relpath(old_release, active.parent), target_is_directory=True,
    )
    _write_json(state_root / ".horizon/manifest.json", {"old": True})
    _write_json(state_root / ".horizon/release.json", {"version": "v1"})
    for name, target in {
        "world": state / "world",
        "ops.json": state / "ops.json",
        "optional.properties": state / "optional.properties",
        "config": state / ".versions/v2/config",
        "logs": state / ".versions/v2/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document, digest = _document("v2", libraries)
    manifest = tmp_path / "stage/manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {
        "active": False, "version": "v2", "manifest_sha256": digest,
    })
    monkeypatch.setattr(promote, "_inactive", lambda _context: None)
    monkeypatch.setattr(promote, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))
    context = promote.PromotionContext(
        "v2", digest, candidate.parent, candidate, manifest, releases,
        releases / "v2", state_root, active, tmp_path / "slot", libraries,
        _post_action_guard(refused),
    )
    return context, old_release


def _bind_installed_version(monkeypatch, context: promote.PromotionContext) -> None:
    monkeypatch.setattr(update, "STATE_ROOT", context.state_root)
    monkeypatch.setattr(update, "RELEASE_ROOT", context.release_root)
    monkeypatch.setattr(update, "ACTIVE_LINK", context.active_link)


@pytest.mark.parametrize("refused", [
    promote.PublicationAction.RELEASE,
    promote.PublicationAction.STATE,
    promote.PublicationAction.ACTIVE_LINK,
])
def test_fresh_post_action_failure_is_bounded_and_retry_converges(
    tmp_path: Path, monkeypatch, refused: promote.PublicationAction,
) -> None:
    context = _fresh_context(tmp_path, monkeypatch, refused)
    assert not (context.candidate / "state/optional.properties").exists()
    with pytest.raises(promote.PublicationRefused, match="post-action ownership loss"):
        promote.promote(context)
    _bind_installed_version(monkeypatch, context)
    expected = "v1" if refused is promote.PublicationAction.ACTIVE_LINK else None
    assert update._installed_version() == expected

    report = promote.promote(context)

    assert report["version"] == "v1"
    assert context.active_link.resolve() == context.release.resolve()
    assert update._installed_version() == "v1"
    assert not (context.candidate / "runtime").exists()
    assert not (context.candidate / "state").exists()


def test_fresh_state_resume_rejects_missing_required_member(
    tmp_path: Path, monkeypatch,
) -> None:
    context = _fresh_context(
        tmp_path, monkeypatch, promote.PublicationAction.STATE,
    )
    with pytest.raises(promote.PublicationRefused, match="post-action ownership loss"):
        promote.promote(context)
    (context.state_root / "ops.json").unlink()
    _bind_installed_version(monkeypatch, context)

    with pytest.raises(promote.PromotionError, match="fresh state member set"):
        promote.promote(context)

    assert not context.active_link.exists()
    assert update._installed_version() is None


@pytest.mark.parametrize("refused", [
    promote.PublicationAction.RELEASE,
    promote.PublicationAction.VERSION_STATE,
    promote.PublicationAction.ACTIVE_LINK,
    promote.PublicationAction.METADATA,
])
def test_upgrade_post_action_failure_is_bounded_and_retry_converges(
    tmp_path: Path, monkeypatch, refused: promote.PublicationAction,
) -> None:
    context, old_release = _upgrade_context(tmp_path, monkeypatch, refused)
    with pytest.raises(promote.PublicationRefused, match="post-action ownership loss"):
        promote.promote(context)
    metadata = json.loads(
        (context.state_root / ".horizon/release.json").read_text(encoding="utf-8"),
    )
    if refused in (
        promote.PublicationAction.RELEASE,
        promote.PublicationAction.VERSION_STATE,
    ):
        assert context.active_link.resolve() == old_release.resolve()
        assert metadata["version"] == "v1"
    elif refused is promote.PublicationAction.ACTIVE_LINK:
        assert context.active_link.resolve() == context.release.resolve()
        assert metadata["version"] == "v1"
    else:
        assert context.active_link.resolve() == context.release.resolve()
        assert metadata["version"] == "v2"
    _bind_installed_version(monkeypatch, context)
    expected = (
        "v1" if refused in (
            promote.PublicationAction.RELEASE,
            promote.PublicationAction.VERSION_STATE,
        )
        else None if refused is promote.PublicationAction.ACTIVE_LINK
        else "v2"
    )
    assert update._installed_version() == expected

    report = promote.promote(context)

    assert report["version"] == "v2"
    assert context.active_link.resolve() == context.release.resolve()
    assert update._installed_version() == "v2"
    assert not (context.candidate / "runtime").exists()
    assert not (context.candidate / "state").exists()


@pytest.mark.parametrize("collision", [
    "duplicate-candidate", "root-symlink", "external-symlink",
    "special-file", "unexpected-member",
])
def test_fresh_state_resume_rejects_ambiguous_or_untrusted_production(
    tmp_path: Path, monkeypatch, collision: str,
) -> None:
    context = _fresh_context(
        tmp_path, monkeypatch, promote.PublicationAction.STATE,
    )
    with pytest.raises(promote.PublicationRefused, match="post-action ownership loss"):
        promote.promote(context)
    if collision == "duplicate-candidate":
        shutil.copytree(
            context.state_root, context.candidate / "state", symlinks=True,
        )
    elif collision == "root-symlink":
        moved = context.state_root.with_name("state-moved")
        context.state_root.rename(moved)
        context.state_root.symlink_to(moved, target_is_directory=True)
    elif collision == "external-symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        (context.state_root / "world/external").symlink_to(
            outside, target_is_directory=True,
        )
    elif collision == "special-file":
        os.mkfifo(context.state_root / "world/fifo", mode=0o640)
    else:
        unexpected = context.state_root / "unexpected"
        unexpected.mkdir()
        unexpected.chmod(0o750)
    _bind_installed_version(monkeypatch, context)

    with pytest.raises(promote.PromotionError, match="collide|fresh state"):
        promote.promote(context)

    assert not context.active_link.exists()
    assert not context.active_link.is_symlink()
    assert update._installed_version() is None
