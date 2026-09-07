from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control import sunlit_update as MODULE


ROOT = Path(__file__).parents[1]
def test_discovers_latest_exact_official_server_pack(monkeypatch) -> None:
    calls = []

    def fetch(url: str):
        calls.append(url)
        if url.endswith("/files"):
            return {
                "data": [
                    {"id": 10, "releaseType": 2, "fileStatus": 4, "hasServerPack": True, "gameVersions": ["1.20.1", "Forge"]},
                    {"id": 20, "releaseType": 1, "fileStatus": None, "hasServerPack": True, "gameVersions": ["1.20.1", "Forge"]},
                ]
            }
        return {
            "data": [
                {
                    "id": 21,
                    "fileName": "SERVER-PACK-Society-Sunlit-Cobblemon-1.2.3-SSV4.1.4.zip",
                    "fileLength": 123456,
                }
            ]
        }

    monkeypatch.setattr(MODULE, "_fetch_json", fetch)
    release = MODULE.discover()

    assert release["version"] == "1.2.3-SSV4.1.4"
    assert release["file_id"] == "21"
    assert calls[-1].endswith("/files/20/additional-files")


def test_check_reports_current_without_mutation(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state/.horizon"
    state.mkdir(parents=True)
    (state / "release.json").write_text(json.dumps({"version": "v2"}), encoding="utf-8")
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v2").mkdir()
    active = tmp_path / "active"
    active.symlink_to(releases / "v2", target_is_directory=True)
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", releases)
    monkeypatch.setattr(MODULE, "ACTIVE_LINK", active)
    monkeypatch.setattr(MODULE, "discover", lambda: {"version": "v2"})
    monkeypatch.setattr(MODULE, "_stage", lambda _release: (_ for _ in ()).throw(AssertionError("must not stage")))

    assert MODULE.run(check_only=False) == {"state": "current", "installed": "v2", "available": None}


def test_installed_version_requires_active_release_commit(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state/.horizon"
    state.mkdir(parents=True)
    (state / "release.json").write_text(json.dumps({"version": "v2"}), encoding="utf-8")
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "v2").mkdir()
    active = tmp_path / "active"
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", releases)
    monkeypatch.setattr(MODULE, "ACTIVE_LINK", active)
    assert MODULE._installed_version() is None
    active.symlink_to(releases / "v2", target_is_directory=True)
    assert MODULE._installed_version() == "v2"


def test_inactive_gate_defers_before_staging(monkeypatch) -> None:
    monkeypatch.setattr(MODULE, "discover", lambda: {"version": "v2", "file_id": "2"})
    monkeypatch.setattr(MODULE, "_installed_version", lambda: "v1")
    monkeypatch.setattr(MODULE, "_inactive", lambda: False)
    monkeypatch.setattr(MODULE, "_stage", lambda _release: (_ for _ in ()).throw(AssertionError("must not stage")))
    monkeypatch.setattr(MODULE.os, "geteuid", lambda: 0)

    assert MODULE.run(check_only=False) == {"state": "deferred", "installed": "v1", "available": "v2"}


def test_inactive_systemd_failure_is_bounded(monkeypatch) -> None:
    def broken_run(*_args, **_kwargs):
        raise OSError("systemd unavailable")

    monkeypatch.setattr(MODULE.subprocess, "run", broken_run)
    with pytest.raises(MODULE.UpdateError, match="inactive state"):
        MODULE._inactive()


def test_inactive_database_failure_is_bounded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(MODULE, "SLOT", tmp_path / "slot.json")
    monkeypatch.setattr(MODULE, "DATABASE", tmp_path / "state.db")
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"),
    )

    def broken_connect(*_args, **_kwargs):
        raise MODULE.sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(MODULE.sqlite3, "connect", broken_connect)
    with pytest.raises(MODULE.UpdateError, match="inactive state"):
        MODULE._inactive()


def test_space_preflight_accounts_for_compressed_archive_expansion_and_old_releases(
    monkeypatch, tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "world.dat").write_bytes(b"state")
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "old.jar").write_bytes(b"old")
    (releases / "libraries").symlink_to(tmp_path / "shared-libraries", target_is_directory=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    overlay = tmp_path / "overlay.jar"
    overlay.write_bytes(b"overlay")
    monkeypatch.setattr(MODULE, "STATE_ROOT", state)
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", releases)
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    monkeypatch.setattr(MODULE, "OVERLAY", overlay)
    monkeypatch.setattr(MODULE, "MAX_TOTAL_SIZE", 100)
    monkeypatch.setattr(MODULE, "SPACE_MARGIN", 10)
    required = 5 + 3 * 100 + 3 * 5 + 2 * 7 + 10
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _path: SimpleNamespace(free=required - 1))

    assert not MODULE._space_available({"size": 5})
    monkeypatch.setattr(MODULE.shutil, "disk_usage", lambda _path: SimpleNamespace(free=required))
    assert MODULE._space_available({"size": 5})


def test_space_preflight_rejects_unproven_archive_size(monkeypatch) -> None:
    with pytest.raises(MODULE.UpdateError, match="free space"):
        MODULE._space_available({"size": "unknown"})


def test_space_preflight_rejects_oversized_archive_directly(monkeypatch, tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.jar"
    overlay.write_bytes(b"overlay")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", tmp_path / "releases")
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    monkeypatch.setattr(MODULE, "OVERLAY", overlay)
    with pytest.raises(MODULE.UpdateError, match="free space"):
        MODULE._space_available({"size": MODULE.MAX_ARCHIVE + 1})


def test_staged_operation_id_is_durable_and_rejects_populated_legacy_root(tmp_path: Path, monkeypatch) -> None:
    staging = tmp_path / "staging"
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    release = {"version": "v2"}
    root = MODULE._staging_root(release)
    first = MODULE._staged_operation_id(root)
    assert MODULE._staged_operation_id(root) == first
    assert str(MODULE.uuid.UUID(first)) == first
    (root / "update-operation-id").write_text("not-a-uuid\n", encoding="ascii")
    (root / "update-operation-id").chmod(0o600)
    with pytest.raises(MODULE.UpdateError, match="identity is malformed"):
        MODULE._staged_operation_id(root)
    (root / "update-operation-id").unlink()
    (root / "candidate").mkdir()
    with pytest.raises(MODULE.UpdateError, match="identity is missing"):
        MODULE._staged_operation_id(root)


@pytest.mark.parametrize("unsafe", ["symlink", "fifo"])
def test_space_preflight_rejects_unsafe_state_members(tmp_path: Path, monkeypatch, unsafe: str) -> None:
    state = tmp_path / "state"
    state.mkdir()
    if unsafe == "symlink":
        (state / "world").symlink_to(tmp_path / "outside")
    else:
        os.mkfifo(state / "world")
    overlay = tmp_path / "overlay.jar"
    overlay.write_bytes(b"overlay")
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(MODULE, "STATE_ROOT", state)
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", tmp_path / "releases")
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    monkeypatch.setattr(MODULE, "OVERLAY", overlay)
    with pytest.raises(MODULE.UpdateError, match="persistent state"):
        MODULE._space_available({"size": 1})


@pytest.mark.parametrize("unsafe", ["symlink", "fifo", "writable"])
def test_space_preflight_rejects_unsafe_overlay(tmp_path: Path, monkeypatch, unsafe: str) -> None:
    state = tmp_path / "state"
    state.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    overlay = tmp_path / "overlay.jar"
    if unsafe == "symlink":
        overlay.symlink_to(tmp_path / "outside.jar")
    elif unsafe == "fifo":
        os.mkfifo(overlay)
    else:
        overlay.write_bytes(b"overlay")
        overlay.chmod(0o666)
    monkeypatch.setattr(MODULE, "STATE_ROOT", state)
    monkeypatch.setattr(MODULE, "RELEASE_ROOT", tmp_path / "releases")
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    monkeypatch.setattr(MODULE, "OVERLAY", overlay)
    with pytest.raises(MODULE.UpdateError, match="overlay size"):
        MODULE._space_available({"size": 1})


def test_update_lease_pause_fully_drains_renewal_thread(monkeypatch) -> None:
    entered = threading.Event()
    unblock = threading.Event()

    class Store:
        def renew_if_owned(self, *_args, **_kwargs):
            entered.set()
            assert unblock.wait(timeout=2)

    monkeypatch.setattr(MODULE, "RESERVATION_RENEW_INTERVAL", 0)
    lease = MODULE._UpdateLease(
        Store(), "operation", 0, controller_pid=os.getpid(),
        controller_start_ticks=1,
    )
    lease.start()
    assert entered.wait(timeout=2)
    pause_done = threading.Event()

    def pause() -> None:
        lease.pause()
        pause_done.set()

    waiter = threading.Thread(target=pause)
    waiter.start()
    time.sleep(0.05)
    assert not pause_done.is_set()
    unblock.set()
    waiter.join(timeout=2)
    assert pause_done.is_set()
    assert lease._thread is None


def test_space_preflight_rejects_unproven_measured_expansion(monkeypatch) -> None:
    with pytest.raises(MODULE.UpdateError, match="expansion"):
        MODULE._space_available(
            {"size": 5},
            manifest={"archive": {"total_uncompressed_size": "unknown"}},
        )


def test_package_update_has_no_retired_manifest_stage_or_promote_dispatch() -> None:
    source = Path(MODULE.__file__).read_text(encoding="utf-8")
    assert "MANIFEST_HELPER" not in source
    assert "STAGE_HELPER" not in source
    assert "PROMOTE_HELPER" not in source
    assert "horizon-sunlit-manifest" not in source
    assert "horizon-sunlit-stage" not in source
    assert "horizon-sunlit-promote" not in source


def test_retired_sunlit_front_doors_are_not_kept_as_source_authority() -> None:
    for name in (
        "horizon-sunlit-manifest",
        "horizon-sunlit-stage",
        "horizon-sunlit-promote",
    ):
        assert not (ROOT / "ops/bin" / name).exists()


def test_stage_uses_package_policy_without_retired_helper_spawn(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "server-pack.zip"
    archive.write_bytes(b"archive")
    overlay = tmp_path / "overlay.jar"
    overlay.write_bytes(b"overlay")
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = {
        "manifest_version": 1,
        "profile_id": "minecraft-sunlit-cobblemon",
        "artifact": {
            "project_id": MODULE.PROJECT_ID,
            "file_id": "42",
            "version": "v2",
            "archive": {"size": archive.stat().st_size, "sha256": MODULE.hashlib.sha256(archive.read_bytes()).hexdigest()},
        },
        "manifest_sha256": "a" * 64,
    }
    monkeypatch.setattr(MODULE, "STAGING_ROOT", staging)
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "OVERLAY", overlay)
    monkeypatch.setattr(MODULE, "make_manifest", lambda _args: manifest)
    monkeypatch.setattr(MODULE, "_space_available", lambda _release, **_kwargs: True)
    monkeypatch.setattr(MODULE, "_download", lambda _release, target: (target.write_bytes(b"archive"), MODULE.hashlib.sha256(b"archive").hexdigest())[1])
    monkeypatch.setattr(MODULE, "_run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retired helper spawned")))

    def fake_stage(args):
        args.candidate_root.mkdir()
        (args.candidate_root / "candidate.json").write_text(
            json.dumps({"version": "v2", "manifest_sha256": "a" * 64}), encoding="utf-8"
        )
        return {"active": False}

    monkeypatch.setattr(MODULE, "stage", fake_stage)
    root, loaded = MODULE._stage({"version": "v2", "file_id": "42", "size": archive.stat().st_size, "url": "https://example.invalid/42"})
    assert root == staging / "sunlit-v2"
    assert loaded["manifest_sha256"] == "a" * 64


def test_systemd_timer_and_installer_are_wired() -> None:
    service = (ROOT / "ops/systemd/horizon-sunlit-auto-update.service").read_text(encoding="utf-8")
    timer = (ROOT / "ops/systemd/horizon-sunlit-auto-update.timer").read_text(encoding="utf-8")
    from game_control.deployment_manifest import get_manifest

    installed_names = {
        spec.target.rsplit("/", 1)[-1]
        for spec in get_manifest().files
        if spec.target.startswith("/usr/local/libexec/")
    }
    assert "ExecStart=/usr/local/libexec/horizon-sunlit-auto-update" in service
    assert "TimeoutStartSec=4h" in service
    assert "OnCalendar=*-*-* 05:00:00 America/New_York" in timer
    assert "Persistent=true" in timer
    for name in (
        "horizon-sunlit-auto-update",
        "horizon-sunlit-update-rpc",
    ):
        assert name in installed_names
    assert "horizon-sunlit-manifest" not in installed_names
    assert "horizon-sunlit-stage" not in installed_names


def test_installed_updater_uses_deployed_venv_interpreter() -> None:
    helper = (ROOT / "ops/bin/horizon-sunlit-auto-update").read_text(encoding="utf-8")
    assert helper.splitlines()[0] == "#!/opt/game-control/.venv/bin/python"
    assert "from game_control.sunlit_update import main" in helper
