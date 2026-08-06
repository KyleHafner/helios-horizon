from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from game_control.backups import BackupService, RestoreService
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


def _profile(tmp_path: Path) -> Profile:
    data, backup = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    return Profile(
        id=ProfileId.TERRARIA_TMOD,
        display_name="tMod",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="terraria-tmod.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=7778),),
        start_timeout_seconds=5,
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
        paths=PathSpec(
            data_roots=(data,),
            mutable_root=data,
            backup_root=backup,
            install_root=data,
            version_file=data / "version",
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.BACKUP, OperationName.RESTORE}),
        update=UpdateSpec(kind="manual"),
    )


def _archive(path: Path, member: str, kind: str = "file") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as archive:
        if kind == "symlink":
            info = tarfile.TarInfo(member)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        elif kind == "fifo":
            info = tarfile.TarInfo(member)
            info.type = tarfile.FIFOTYPE
            archive.addfile(info)
        else:
            payload = b"restored"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, __import__("io").BytesIO(payload))
    return path


@pytest.mark.parametrize("member", ["/absolute", "../escape", "nested/../../escape"])
def test_restore_rejects_unsafe_member_before_destination_touch(tmp_path: Path, member: str):
    profile = _profile(tmp_path)
    sentinel = profile.paths.mutable_root / "sentinel"
    sentinel.write_text("keep")
    archive = _archive(profile.paths.backup_root / "bad.tar", member)

    with pytest.raises(SafeError):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_restore_rejects_link_and_special_members(tmp_path: Path, kind: str):
    profile = _profile(tmp_path)
    sentinel = profile.paths.mutable_root / "sentinel"
    sentinel.write_text("keep")
    archive = _archive(profile.paths.backup_root / "bad.tar", "world", kind)
    with pytest.raises(SafeError):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert sentinel.read_text() == "keep"


def test_restore_rejects_running_profile_and_wrong_profile(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = _archive(profile.paths.backup_root / "bad.tar", "world")
    with pytest.raises(SafeError, match="running"):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: False).restore(archive)


def test_restore_decompressor_failure_cleans_private_staging(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = profile.paths.backup_root / "broken.tar.zst"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"not-a-zstd-stream")
    with pytest.raises(SafeError):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert not list(profile.paths.backup_root.glob(".zstd-*"))


def test_restore_valid_archive_stages_and_retains_rollback_until_health(tmp_path: Path):
    profile = _profile(tmp_path)
    current = profile.paths.mutable_root / "world"
    current.write_text("old")
    backup = BackupService(profile, stopped_check=lambda: True).create()
    current.write_text("changed")
    service = RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True)
    import game_control.backups as backups
    popens = []
    original_popen = backups.subprocess.Popen
    original_tarfile_open = backups.tarfile.open
    monkeypatch = pytest.MonkeyPatch()
    def force_external(*args, **kwargs):
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        if args and isinstance(args[0], Path) and mode == "r:*":
            raise tarfile.ReadError("exercise the external zstd fallback")
        return original_tarfile_open(*args, **kwargs)

    monkeypatch.setattr(
        backups.subprocess,
        "Popen",
        lambda *args, **kwargs: (popens.append(args[0]), original_popen(*args, **kwargs))[1],
    )
    monkeypatch.setattr(backups.tarfile, "open", force_external)

    result = service.restore(backup.path)
    monkeypatch.undo()

    assert (result.destination / "world").read_text() == "old"
    assert len(popens) == 1
    assert stat.S_IMODE(result.destination.stat().st_mode) == 0o750
    assert stat.S_IMODE((result.destination / "world").stat().st_mode) == 0o640
    assert result.rollback is not None and result.rollback.exists()
    service.finalize(result)
    assert not result.rollback.exists()


class _NoopBackup:
    def create(self, **_kwargs):
        return None
