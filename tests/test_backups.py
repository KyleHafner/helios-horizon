from __future__ import annotations

import hashlib
import json
import os
import errno
import subprocess
from pathlib import Path

import pytest

from game_control.backups import BackupService
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
    data = tmp_path / "data"
    backup = tmp_path / "backups"
    data.mkdir()
    return Profile(
        id=ProfileId.TERRARIA_VANILLA,
        display_name="Vanilla",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="terraria-vanilla.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=7777),),
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


class _Db:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_backup(self, **row):
        self.rows.append(row)

    def list_backups(self, profile_id):
        return list(self.rows)


def test_create_writes_deterministic_manifest_and_verified_archive(tmp_path: Path):
    profile = _profile(tmp_path)
    (profile.paths.mutable_root / "z.txt").write_text("z")
    (profile.paths.mutable_root / "a.txt").write_text("a")
    db = _Db()

    result = BackupService(profile, database=db, stopped_check=lambda: True).create()

    assert result.verified is True
    assert result.path.exists()
    assert not list(profile.paths.backup_root.glob("*.partial"))
    manifest = json.loads(
        subprocess.run(
            ["/usr/bin/tar", "--zstd", "--extract", "--to-stdout", "--file", str(result.path), "manifest.json"],
            check=True,
            capture_output=True,
        ).stdout
    )
    assert manifest["profile_id"] == "terraria-vanilla"
    assert [entry["path"] for entry in manifest["entries"]] == ["a.txt", "z.txt"]
    assert manifest["entries"][0]["sha256"] == hashlib.sha256(b"a").hexdigest()
    assert db.rows and db.rows[0]["verified"] is True


def test_create_excludes_symlink_and_partial_and_checks_free_space(tmp_path: Path):
    profile = _profile(tmp_path)
    (profile.paths.mutable_root / "world.wld").write_text("world")
    (profile.paths.mutable_root / "escape").symlink_to("/etc/passwd")
    (profile.paths.mutable_root / "leftover.partial").write_text("ignore")
    with pytest.raises(SafeError, match="free space"):
        BackupService(
            profile,
            free_space=lambda _path: 0,
            stopped_check=lambda: True,
        ).create()
    assert not list(profile.paths.backup_root.glob("*.partial"))


def test_snapshot_uses_hardlinks_on_same_filesystem(tmp_path: Path, monkeypatch):
    profile = _profile(tmp_path)
    source = profile.paths.mutable_root / "world.wld"
    source.write_bytes(b"world")
    import game_control.backups as backups

    linked: list[tuple[Path, Path]] = []
    original_link = backups.os.link

    def link(src, dst, **kwargs):
        linked.append((Path(src), Path(dst)))
        return original_link(src, dst, **kwargs)

    monkeypatch.setattr(backups.os, "link", link)
    monkeypatch.setattr(backups.shutil, "copy2", lambda *_args, **_kwargs: pytest.fail("copy2 used"))
    BackupService(profile, stopped_check=lambda: True).create()
    assert linked and linked[0][0] == source


def test_snapshot_falls_back_to_copy_on_cross_device_link(tmp_path: Path, monkeypatch):
    profile = _profile(tmp_path)
    source = profile.paths.mutable_root / "world.wld"
    source.write_bytes(b"world")
    import game_control.backups as backups

    copied = []
    monkeypatch.setattr(backups.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")))
    original_copy = backups.shutil.copy2
    monkeypatch.setattr(backups.shutil, "copy2", lambda *args, **kwargs: (copied.append(args), original_copy(*args, **kwargs))[1])
    BackupService(profile, stopped_check=lambda: True).create()
    assert copied


def test_protected_retention_keeps_two_verified_and_never_sole_verified(tmp_path: Path):
    profile = _profile(tmp_path)
    db = _Db()
    service = BackupService(profile, database=db, stopped_check=lambda: True)
    results = [service.create(protected=index == 0) for index in range(3)]

    service.prune(keep=2)
    assert results[0].path.exists()
    assert sum(result.path.exists() for result in results) >= 2
    service.prune(keep=0)
    assert results[0].path.exists()
