from __future__ import annotations

import os
import sqlite3
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from game_control.errors import SafeError
from game_control.models import (
    AdapterKind,
    OperationName,
    PathSpec,
    PortSpec,
    ProcessSpec,
    Profile,
    ProfileId,
    UpdateSpec,
)
from game_control.updates import UpdateService


def _profile(tmp_path: Path, profile_id=ProfileId.TERRARIA_TMOD, kind="release_symlink"):
    data = tmp_path / "data"
    install = tmp_path / "install"
    backup = tmp_path / "backups"
    data.mkdir()
    install.mkdir()
    return Profile(
        id=profile_id,
        display_name="Game",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="game.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=7777),),
        start_timeout_seconds=5,
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
        paths=PathSpec(
            data_roots=(data,),
            mutable_root=data,
            backup_root=backup,
            install_root=install,
            version_file=install / "version",
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset(
            {OperationName.UPDATE_CHECK}
            if kind == "manual"
            else {OperationName.UPDATE_CHECK, OperationName.UPDATE_APPLY}
        ),
        update=UpdateSpec(
            kind=kind,
            download_url="https://updates.example.invalid/release",
            executable_relative_path="game",
            version_command=("/usr/bin/printf", "2.0"),
        )
        if kind == "release_symlink"
        else UpdateSpec(kind=kind, app_id=380870) if kind == "steamcmd_in_place" else UpdateSpec(kind="manual"),
    )


class _Backup:
    def __init__(self, path: Path, verified: bool = True):
        self.path, self.verified, self.id = path, verified, "backup-id"

    def create(self, **_kwargs):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"full rollback archive")
        return self


def test_release_update_atomically_replaces_current_and_keeps_prior(tmp_path: Path):
    profile = _profile(tmp_path)
    releases = profile.paths.install_root / "releases"
    prior = releases / "prior"
    prior.mkdir(parents=True)
    (prior / "game").write_text("old")
    current = profile.paths.install_root / "current"
    current.symlink_to(prior, target_is_directory=True)

    def stage(_profile, staging: Path):
        (staging / "game").write_text("new")
        return "new"

    service = UpdateService(
        profiles={profile.id.value: profile},
        backup_service=_Backup(tmp_path / "backups" / "pre.tar.zst"),
        stage_release=stage,
        verify_release=lambda _p, _r: True,
        stopped_check=lambda _p: True,
    )
    result = service.apply(profile.id)
    assert result.state == "succeeded"
    assert current.resolve().name != "prior"
    assert prior.exists()


def test_failed_release_verification_rolls_back_and_records_both_outcomes(tmp_path: Path):
    profile = _profile(tmp_path)
    releases = profile.paths.install_root / "releases"
    prior = releases / "prior"
    prior.mkdir(parents=True)
    (prior / "game").write_text("old")
    current = profile.paths.install_root / "current"
    current.symlink_to(prior, target_is_directory=True)

    service = UpdateService(
        profiles={profile.id.value: profile},
        backup_service=_Backup(tmp_path / "pre.tar.zst"),
        stage_release=lambda _p, staging: (staging / "game").write_text("new") or "new",
        verify_release=lambda _p, _r: False,
        stopped_check=lambda _p: True,
        database=sqlite3.connect(":memory:"),
    )
    service.database.execute(
        "CREATE TABLE updates(id TEXT,profile_id TEXT,created_at TEXT,strategy TEXT,prior_version TEXT,new_version TEXT,state TEXT)"
    )
    with pytest.raises(SafeError, match="verification"):
        service.apply(profile.id)
    assert current.resolve() == prior
    states = [row[0] for row in service.database.execute("SELECT state FROM updates")]
    assert states == ["failed", "rolled_back"]


def test_running_profile_and_missing_backup_abort_before_updater(tmp_path: Path):
    profile = _profile(tmp_path)
    called = False

    def updater(_argv):
        nonlocal called
        called = True

    service = UpdateService(
        profiles={profile.id.value: profile},
        backup_service=None,
        runner=updater,
        stopped_check=lambda _p: False,
    )
    with pytest.raises(SafeError, match="running"):
        service.apply(profile.id)
    assert called is False


def test_minecraft_is_manual_only(tmp_path: Path):
    profile = _profile(tmp_path, ProfileId.MINECRAFT, "manual")
    called = False
    service = UpdateService(
        profiles={profile.id.value: profile},
        runner=lambda _argv: called,
        stopped_check=lambda _p: True,
    )
    result = service.apply(profile.id)
    assert result.state == "manual_only"
    assert called is False


def test_pz_uses_fixed_steamcmd_argv(tmp_path: Path):
    profile = _profile(tmp_path, ProfileId.PZ_RISING, "steamcmd_in_place")
    seen: list[list[str]] = []
    service = UpdateService(
        profiles={profile.id.value: profile},
        backup_service=_Backup(tmp_path / "pre.tar.zst"),
        runner=lambda argv, **_kwargs: seen.append(argv),
        stopped_check=lambda _p: True,
    )
    result = service.apply(profile.id)
    assert result.state == "succeeded"
    assert seen == [[
        "/opt/steamcmd/steamcmd.sh", "+force_install_dir", "/opt/pzserver",
        "+login", "anonymous", "+app_update", "380870", "-beta", "unstable",
        "validate", "+quit",
    ]]


def test_release_update_rejects_external_plain_http():
    with pytest.raises(ValueError, match="require HTTPS"):
        UpdateSpec(
            kind="release_symlink",
            download_url="http://updates.example.com/release.tar.zst",
            executable_relative_path="game",
        )


def test_release_update_allows_loopback_http_for_local_validation():
    spec = UpdateSpec(
        kind="release_symlink",
        download_url="http://127.0.0.1:8080/release.tar.zst",
        executable_relative_path="game",
    )
    assert spec.download_url is not None
    assert spec.download_url.host == "127.0.0.1"


def test_tar_release_rejects_fifo_member(tmp_path: Path):
    archive_path = tmp_path / "release.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("game")
        member.type = tarfile.FIFOTYPE
        archive.addfile(member)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(SafeError, match="release archive is invalid"):
        UpdateService(profiles={})._safe_extract(archive_path, staging, "game")


def test_zip_release_rejects_fifo_member(tmp_path: Path):
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        member = zipfile.ZipInfo("game")
        member.create_system = 3
        member.external_attr = (stat.S_IFIFO | 0o600) << 16
        archive.writestr(member, b"")
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(SafeError, match="release archive is invalid"):
        UpdateService(profiles={})._safe_extract(archive_path, staging, "game")
