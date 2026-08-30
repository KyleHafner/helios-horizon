from __future__ import annotations

import json
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
    monkeypatch.setattr(MODULE, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(MODULE, "discover", lambda: {"version": "v2"})
    monkeypatch.setattr(MODULE, "_stage", lambda _release: (_ for _ in ()).throw(AssertionError("must not stage")))

    assert MODULE.run(check_only=False) == {"state": "current", "installed": "v2", "available": None}


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
