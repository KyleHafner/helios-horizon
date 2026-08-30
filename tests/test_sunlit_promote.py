from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

from game_control import sunlit_promote as MODULE


ROOT = Path(__file__).parents[1]
def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def test_promotes_exact_inactive_candidate_into_fixed_layout(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage"
    candidate = stage / "candidate-v2"
    runtime = candidate / "runtime"
    state = candidate / "state"
    release_root = tmp_path / "opt/releases"
    active = tmp_path / "srv/current"
    state_root = tmp_path / "srv/state"
    libraries = tmp_path / "opt/libraries"
    for path in (runtime, state / "world", state / ".versions/v1/config", state / ".versions/v1/logs", release_root, active.parent, libraries):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods").mkdir()
    (runtime / "mods/new.jar").write_bytes(b"new")
    (runtime / "mods/new.jar").chmod(0o640)
    (state / "world/level.dat").write_bytes(b"world")
    (state / "ops.json").write_bytes(b"ops")
    for name, target in {
        "world": state / "world",
        "ops.json": state / "ops.json",
        "config": state / ".versions/v1/config",
        "logs": state / ".versions/v1/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": "v1", "archive": {"sha256": "a" * 64}},
        "runtime_policy": {
            "persistent_dirs": ["world"],
            "persistent_files": ["ops.json"],
            "mutable_vendor_dirs": ["config"],
            "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    manifest_sha = hashlib.sha256(canonical).hexdigest()
    document["manifest_sha256"] = manifest_sha
    manifest = stage / "manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {"active": False, "version": "v1", "manifest_sha256": manifest_sha})
    globals_ = MODULE.promote.__globals__
    replacements = {
        "VERSION": "v1",
        "MANIFEST_SHA256": manifest_sha,
        "STAGING_ROOT": stage,
        "CANDIDATE": candidate,
        "MANIFEST": manifest,
        "RELEASE_ROOT": release_root,
        "RELEASE": release_root / "v1",
        "STATE_ROOT": state_root,
        "ACTIVE_LINK": active,
        "SLOT": tmp_path / "slot.json",
        "LIBRARIES": libraries,
    }
    for name, value in replacements.items():
        monkeypatch.setitem(globals_, name, value)
    monkeypatch.setattr(globals_["subprocess"], "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"))
    monkeypatch.setitem(globals_, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))

    report = MODULE.promote()

    assert report["active"] is True
    assert active.is_symlink() and active.resolve() == (release_root / "v1").resolve()
    assert (active / "world/level.dat").read_bytes() == b"world"
    assert (active / "ops.json").read_bytes() == b"ops"
    assert os.readlink(active / "libraries") == str(libraries)
    assert (active / "mods/new.jar").stat().st_mode & 0o777 == 0o644
    assert (state_root / ".horizon/manifest.json").is_file()
    assert not (candidate / "runtime").exists()
    assert not (candidate / "state").exists()
    assert MODULE.promote() == report


def test_upgrades_existing_release_without_replacing_stable_state(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage"
    candidate = stage / "candidate"
    runtime = candidate / "runtime"
    candidate_state = candidate / "state"
    release_root = tmp_path / "opt/releases"
    old_release = release_root / "v1"
    new_release = release_root / "v2"
    active = tmp_path / "srv/current"
    state_root = tmp_path / "srv/state"
    libraries = tmp_path / "opt/libraries"
    for path in (
        runtime / "mods",
        candidate_state / "world",
        candidate_state / ".versions/v2/config",
        candidate_state / ".versions/v2/logs",
        old_release,
        state_root / "world",
        state_root / ".versions/v1/config",
        state_root / ".horizon",
        libraries,
    ):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "mods/new.jar").write_bytes(b"new")
    (candidate_state / "world/level.dat").write_bytes(b"world")
    (candidate_state / "ops.json").write_bytes(b"ops")
    (state_root / "world/level.dat").write_bytes(b"world")
    (state_root / "ops.json").write_bytes(b"ops")
    (candidate_state / "world").chmod(0o755)
    (state_root / "world").chmod(0o750)
    (state_root / ".versions/v1/config/old.toml").write_bytes(b"old")
    _write_json(state_root / ".horizon/manifest.json", {"old": True})
    _write_json(state_root / ".horizon/release.json", {"version": "v1"})
    active.symlink_to(os.path.relpath(old_release, active.parent), target_is_directory=True)
    for name, target in {
        "world": candidate_state / "world",
        "ops.json": candidate_state / "ops.json",
        "config": candidate_state / ".versions/v2/config",
        "logs": candidate_state / ".versions/v2/logs",
        "libraries": libraries,
    }.items():
        (runtime / name).symlink_to(target, target_is_directory=target.is_dir())
    document = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {"version": "v2", "archive": {"sha256": "b" * 64}},
        "runtime_policy": {
            "persistent_dirs": ["world"],
            "persistent_files": ["ops.json"],
            "mutable_vendor_dirs": ["config"],
            "empty_mutable_dirs": ["logs"],
            "fixed_symlinks": {"libraries": str(libraries)},
        },
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    manifest_sha = hashlib.sha256(canonical).hexdigest()
    document["manifest_sha256"] = manifest_sha
    manifest = stage / "manifest.json"
    _write_json(manifest, document)
    _write_json(candidate / "candidate.json", {"active": False, "version": "v2", "manifest_sha256": manifest_sha})
    globals_ = MODULE.promote.__globals__
    for name, value in {
        "VERSION": "v2",
        "MANIFEST_SHA256": manifest_sha,
        "STAGING_ROOT": stage,
        "CANDIDATE": candidate,
        "MANIFEST": manifest,
        "RELEASE_ROOT": release_root,
        "RELEASE": new_release,
        "STATE_ROOT": state_root,
        "ACTIVE_LINK": active,
        "SLOT": tmp_path / "slot.json",
        "LIBRARIES": libraries,
    }.items():
        monkeypatch.setitem(globals_, name, value)
    monkeypatch.setattr(globals_["subprocess"], "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"))
    monkeypatch.setitem(globals_, "_sunlit_ids", lambda: (os.getuid(), os.getgid()))

    report = MODULE.promote()

    assert report["version"] == "v2"
    assert active.resolve() == new_release.resolve()
    assert (state_root / "world/level.dat").read_bytes() == b"world"
    assert (state_root / ".versions/v1/config/old.toml").read_bytes() == b"old"
    assert (state_root / ".versions/v2/config").is_dir()
    assert (state_root / ".horizon/release.json").stat().st_mode & 0o777 == 0o640
    assert (new_release / "world").resolve() == (state_root / "world").resolve()
    assert not (candidate / "runtime").exists()
    assert not (candidate / "state").exists()
    assert MODULE.promote() == report
