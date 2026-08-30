from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
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
        elif kind == "directory":
            info = tarfile.TarInfo(member)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        else:
            payload = b"restored"
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            archive.addfile(info, __import__("io").BytesIO(payload))
    return path


def _manifest_archive(
    path: Path,
    profile: Profile,
    *,
    manifest: dict | None = None,
    duplicate: bool = False,
    payload_uid: int | None = None,
    manifest_uid: int | None = None,
    payload_gid: int | None = None,
    manifest_gid: int | None = None,
) -> Path:
    payload = b"restored"
    uid = os.geteuid() if manifest_uid is None else manifest_uid
    gid = os.getegid() if manifest_gid is None else manifest_gid
    payload_uid = uid if payload_uid is None else payload_uid
    payload_gid = gid if payload_gid is None else payload_gid
    default_manifest = {
        "schema": 1,
        "profile_id": str(profile.id),
        "backup_id": "test-backup",
        "entries": [
            {
                "path": "world",
                "archive_path": "payload/world",
                "size": len(payload),
                "uid": uid,
                "gid": gid,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as archive:
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_bytes = json.dumps(manifest or default_manifest).encode()
        manifest_info.size = len(manifest_bytes)
        manifest_info.uid = uid
        manifest_info.gid = gid
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        payload_info = tarfile.TarInfo("payload/world")
        payload_info.size = len(payload)
        payload_info.uid = payload_uid
        payload_info.gid = payload_gid
        archive.addfile(payload_info, io.BytesIO(payload))
        if duplicate:
            archive.addfile(payload_info, io.BytesIO(payload))
    return path


def _different_id(value: int) -> int:
    return value + 1 if value < (1 << 32) - 1 else value - 1


def _zstd_archive(source: Path, target: Path) -> Path:
    subprocess.run(
        ["/usr/bin/zstd", "-q", "-f", "-o", str(target), str(source)],
        check=True,
        capture_output=True,
    )
    return target


@pytest.mark.parametrize("member", ["/absolute", "../escape", "nested/../../escape"])
def test_restore_rejects_unsafe_member_before_destination_touch(tmp_path: Path, member: str):
    profile = _profile(tmp_path)
    sentinel = profile.paths.mutable_root / "sentinel"
    sentinel.write_text("keep")
    archive = _archive(profile.paths.backup_root / "bad.tar", member)

    with pytest.raises(SafeError):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
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


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("wrong_profile", "wrong_profile"),
        ("non_list_entries", "invalid_backup"),
        ("non_dict_entry", "invalid_backup"),
        ("missing_archive_path", "invalid_backup"),
        ("unsafe_path", "invalid_backup"),
        ("ownership", "ownership_mismatch"),
        ("size", "checksum_mismatch"),
        ("checksum", "checksum_mismatch"),
        ("archive_mismatch", "invalid_backup"),
        ("missing_backup_id", "invalid_backup"),
    ],
)
def test_restore_rejects_manifest_data_guards(tmp_path: Path, case: str, expected_code: str):
    profile = _profile(tmp_path)
    manifest = {
        "schema": 1,
        "profile_id": str(profile.id),
        "backup_id": "test-backup",
        "entries": [
            {
                "path": "world",
                "archive_path": "payload/world",
                "size": 8,
                "uid": os.geteuid(),
                "gid": os.getegid(),
                "sha256": hashlib.sha256(b"restored").hexdigest(),
            }
        ],
    }
    if case == "wrong_profile":
        manifest["profile_id"] = "other-profile"
    elif case == "non_list_entries":
        manifest["entries"] = "not-a-list"
    elif case == "non_dict_entry":
        manifest["entries"] = [None]
    elif case == "missing_archive_path":
        manifest["entries"][0].pop("archive_path")
    elif case == "unsafe_path":
        manifest["entries"][0]["path"] = "../escape"
    elif case == "ownership":
        manifest["entries"][0]["uid"] = os.geteuid() + 1
    elif case == "size":
        manifest["entries"][0]["size"] = 99
    elif case == "checksum":
        manifest["entries"][0]["sha256"] = "0" * 64
    elif case == "archive_mismatch":
        manifest["entries"] = []
    elif case == "missing_backup_id":
        manifest["backup_id"] = ""

    archive = _manifest_archive(profile.paths.backup_root / "guard.tar", profile, manifest=manifest)
    with pytest.raises(SafeError) as raised:
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert raised.value.code == expected_code
    assert not list(tmp_path.glob(".restore-*"))


def test_restore_rejects_duplicate_archive_members_before_destination_touch(tmp_path: Path):
    profile = _profile(tmp_path)
    sentinel = profile.paths.mutable_root / "sentinel"
    sentinel.write_text("keep")
    archive = _manifest_archive(profile.paths.backup_root / "duplicate.tar", profile, duplicate=True)

    with pytest.raises(SafeError) as raised:
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert raised.value.code == "invalid_backup"
    assert sentinel.read_text() == "keep"


def test_restore_rejects_archive_member_ownership_mismatch(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = _manifest_archive(
        profile.paths.backup_root / "ownership.tar",
        profile,
        payload_uid=os.geteuid() + 1,
    )

    with pytest.raises(SafeError) as raised:
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert raised.value.code == "ownership_mismatch"


@pytest.mark.parametrize("compressed", [False, True])
def test_restore_remaps_source_ownership_to_existing_target_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool
):
    profile = _profile(tmp_path)
    target = os.lstat(profile.paths.mutable_root)
    source_uid, source_gid = _different_id(target.st_uid), _different_id(target.st_gid)
    manifest = {
        "schema": 1,
        "profile_id": str(profile.id),
        "backup_id": "portable-ownership",
        "entries": [
            {
                "path": "nested/world",
                "archive_path": "payload/world",
                "size": 8,
                "uid": source_uid,
                "gid": source_gid,
                "sha256": hashlib.sha256(b"restored").hexdigest(),
            }
        ],
    }
    raw = _manifest_archive(
        profile.paths.backup_root / "portable.tar",
        profile,
        manifest=manifest,
        payload_uid=source_uid,
        manifest_uid=source_uid,
        payload_gid=source_gid,
        manifest_gid=source_gid,
    )
    archive = _zstd_archive(raw, profile.paths.backup_root / "portable.tar.zst") if compressed else raw
    if compressed:
        import game_control.backups as backups

        real_open = backups.tarfile.open

        def force_external(*args, **kwargs):
            if kwargs.get("mode") == "r:*":
                raise tarfile.ReadError("test external path")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(backups.tarfile, "open", force_external)

    result = RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)

    assert result.destination.stat().st_uid == target.st_uid
    assert result.destination.stat().st_gid == target.st_gid
    assert (result.destination / "nested").stat().st_uid == target.st_uid
    assert (result.destination / "nested").stat().st_gid == target.st_gid
    assert (result.destination / "nested/world").stat().st_uid == target.st_uid
    assert (result.destination / "nested/world").stat().st_gid == target.st_gid


def test_restore_rejects_untrusted_target_root_before_pre_restore_backup(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = _manifest_archive(profile.paths.backup_root / "valid.tar", profile)
    profile.paths.mutable_root.rmdir()
    profile.paths.mutable_root.write_text("not a directory")
    backup = _RecordingBackup()

    with pytest.raises(SafeError) as raised:
        RestoreService(profile, backup_service=backup, stopped_check=lambda: True).restore(archive)

    assert raised.value.code == "invalid_destination"
    assert backup.calls == []


def test_restore_rejects_unapproved_or_missing_archive(tmp_path: Path):
    profile = _profile(tmp_path)
    outside = _manifest_archive(tmp_path / "outside.tar", profile)
    link = profile.paths.backup_root / "link.tar"
    link.parent.mkdir()
    link.symlink_to(outside)

    service = RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True)
    with pytest.raises(SafeError) as raised:
        service.restore(outside)
    assert raised.value.code == "invalid_backup"
    with pytest.raises(SafeError) as raised:
        service.restore(link)
    assert raised.value.code == "invalid_backup"
    with pytest.raises(SafeError) as raised:
        service.restore(profile.paths.backup_root / "missing.tar")
    assert raised.value.code == "backup_not_found"


def test_restore_checks_free_space_before_creating_pre_restore_backup(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = _manifest_archive(profile.paths.backup_root / "valid.tar", profile)
    backup = _RecordingBackup()
    sentinel = profile.paths.mutable_root / "sentinel"
    sentinel.write_text("keep")

    with pytest.raises(SafeError, match="free space"):
        RestoreService(
            profile,
            backup_service=backup,
            stopped_check=lambda: True,
            free_space=lambda _path: 0,
        ).restore(archive)

    assert backup.calls == []
    assert sentinel.read_text() == "keep"


def test_restore_rejects_symlink_destination_and_cleans_staging(tmp_path: Path):
    profile = _profile(tmp_path)
    archive = _manifest_archive(profile.paths.backup_root / "valid.tar", profile)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    profile.paths.mutable_root.rmdir()
    profile.paths.mutable_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SafeError) as raised:
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)
    assert raised.value.code == "invalid_destination"
    assert sentinel.read_text() == "keep"
    assert not list(tmp_path.glob(".restore-*"))


def test_restore_rolls_back_destination_when_atomic_replace_fails(tmp_path: Path, monkeypatch):
    profile = _profile(tmp_path)
    current = profile.paths.mutable_root / "world"
    current.write_text("old")
    archive = _manifest_archive(profile.paths.backup_root / "valid.tar", profile)
    real_replace = os.replace
    calls = []

    def fail_destination_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        if len(calls) == 2:
            raise OSError("destination replace failed")
        return real_replace(source, destination)

    monkeypatch.setattr("game_control.backups.os.replace", fail_destination_replace)
    with pytest.raises(SafeError, match="could not be completed"):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive)

    assert current.read_text() == "old"
    assert calls
    assert not list(tmp_path.glob(".restore-*"))


def test_restore_health_check_finalizes_rollback(tmp_path: Path):
    profile = _profile(tmp_path)
    current = profile.paths.mutable_root / "world"
    current.write_text("old")
    archive = _manifest_archive(profile.paths.backup_root / "valid.tar", profile)

    result = RestoreService(
        profile,
        backup_service=_NoopBackup(),
        stopped_check=lambda: True,
        health_check=lambda _destination: True,
    ).restore(archive)

    assert result.rollback is not None
    assert not result.rollback.exists()


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
    assert len(popens) == 4
    assert popens[0][0] == "/usr/bin/systemd-run"
    assert "--slice=maintenance.slice" in popens[0] and "--" in popens[0]
    unit = next(item.split("=", 1)[1] for item in popens[0] if item.startswith("--unit="))
    assert popens[1] == ["/usr/bin/systemctl", "show", unit, "--no-legend", "--property=ExecMainStatus", "--property=Result"]
    assert popens[2] == ["/usr/bin/systemctl", "stop", unit]
    assert popens[3] == ["/usr/bin/systemctl", "reset-failed", unit]
    assert stat.S_IMODE(result.destination.stat().st_mode) == 0o750
    assert stat.S_IMODE((result.destination / "world").stat().st_mode) == 0o640
    assert result.rollback is not None and result.rollback.exists()
    service.finalize(result)
    assert not result.rollback.exists()


def test_multiroot_restore_publishes_each_root_to_original_destination(tmp_path: Path):
    profile = _profile(tmp_path)
    second = tmp_path / "install"
    second.mkdir(parents=True)
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={"backup_roots": (profile.paths.mutable_root, second), "data_roots": (profile.paths.mutable_root, second)})})
    (profile.paths.mutable_root / "state.dat").write_text("old-state")
    (second / "release.jar").write_text("old-release")
    archive = BackupService(profile, stopped_check=lambda: True).create()
    (profile.paths.mutable_root / "state.dat").write_text("changed")
    (second / "release.jar").write_text("changed")
    result = RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).restore(archive.path)
    assert (profile.paths.mutable_root / "state.dat").read_text() == "old-state"
    assert (second / "release.jar").read_text() == "old-release"
    assert not (profile.paths.mutable_root / "root-0").exists()
    assert not (second / "root-1").exists()
    RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).finalize(result)


def test_legacy_multiroot_archive_fails_closed_before_backup(tmp_path: Path):
    profile = _profile(tmp_path)
    second = tmp_path / "install"
    second.mkdir(parents=True)
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={"backup_roots": (profile.paths.mutable_root, second), "data_roots": (profile.paths.mutable_root, second)})})
    archive = _manifest_archive(profile.paths.backup_root / "legacy.tar", profile)
    with pytest.raises(SafeError, match="ambiguous"):
        RestoreService(profile, backup_service=_RecordingBackup(), stopped_check=lambda: True).restore(archive)


def test_schema2_root_ids_are_identity_derived(tmp_path: Path):
    import game_control.backups as backups

    profile = _profile(tmp_path)
    second = tmp_path / "install"
    second.mkdir()
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={
        "backup_roots": (profile.paths.mutable_root, second),
        "data_roots": (profile.paths.mutable_root, second),
    })})
    roots = profile.paths.backup_roots
    manifest = {
        "schema": 2,
        "roots": [
            {"id": "forged-root-a", "identity": backups._root_identity(roots[0])},
            {"id": backups._root_id(roots[1]), "identity": backups._root_identity(roots[1])},
        ],
    }
    with pytest.raises(SafeError, match="root mapping"):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True)._read_targets(manifest)


def test_multiroot_health_failure_does_not_finalize_any_root(tmp_path: Path):
    import game_control.backups as backups

    profile = _profile(tmp_path)
    second = tmp_path / "install"
    second.mkdir()
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={
        "backup_roots": (profile.paths.mutable_root, second),
        "data_roots": (profile.paths.mutable_root, second),
    })})
    (profile.paths.mutable_root / "first").write_text("one")
    (second / "second").write_text("two")
    archive = BackupService(profile, stopped_check=lambda: True).create()
    healthy = lambda destination: destination == profile.paths.mutable_root
    result = RestoreService(
        profile, backup_service=_NoopBackup(), stopped_check=lambda: True, health_check=healthy,
    ).restore(archive.path)
    assert result.rollbacks and all(item is not None and item.exists() for item in result.rollbacks)
    assert result.destinations == profile.paths.backup_roots


def test_reconcile_rejects_untrusted_journal_without_touching_paths(tmp_path: Path):
    profile = _profile(tmp_path)
    profile.paths.backup_root.mkdir()
    journal = profile.paths.backup_root / ".restore-journal-evil.json"
    journal.write_text(json.dumps({"phase": "activated", "backup_id": "x", "roots": [{"root_id": "root-0", "destination": "/tmp", "staging": "/tmp/.restore-root-0-x", "rollback": "/tmp/.rollback-x"}]}))
    os.chmod(journal, 0o600)
    with pytest.raises(SafeError, match="journal"):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).reconcile()


def test_multiroot_capacity_is_checked_per_destination(tmp_path: Path):
    profile = _profile(tmp_path)
    second = tmp_path / "other" / "install"
    second.mkdir(parents=True)
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={"backup_roots": (profile.paths.mutable_root, second), "data_roots": (profile.paths.mutable_root, second)})})
    (profile.paths.mutable_root / "state").write_bytes(b"a")
    (second / "release").write_bytes(b"b")
    archive = BackupService(profile, stopped_check=lambda: True).create()
    free = lambda path: 100000 if path == profile.paths.mutable_root.parent else 1
    with pytest.raises(SafeError, match="free space"):
        RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True, free_space=free).restore(archive.path)


@pytest.mark.parametrize(
    ("phase", "count"),
    [("staged", 0), ("displacing", 0), ("displacing", 1), ("displaced", 2),
     ("publishing", 0), ("publishing", 1), ("activated", 2)],
)
def test_reconcile_crash_matrix_restores_existing_and_absent_roots(tmp_path: Path, phase: str, count: int):
    import game_control.backups as backups

    profile = _profile(tmp_path)
    second = tmp_path / "install"
    profile = profile.model_copy(update={"paths": profile.paths.model_copy(update={"backup_roots": (profile.paths.mutable_root, second), "data_roots": (profile.paths.mutable_root, second)})})
    profile.paths.backup_root.mkdir()
    existing = profile.paths.mutable_root
    (existing / "old").write_text("old")
    roots = (existing, second)
    ids = tuple(backups._root_id(root) for root in roots)
    stagings = [root.parent / f".restore-{rid}-crash" for rid, root in zip(ids, roots)]
    rollbacks = [root.parent / f".rollback-crash-{i}" for i, root in enumerate(roots)]
    for staging in stagings:
        staging.mkdir(parents=True)
    displaced = ids[:count] if phase in {"displacing", "displaced"} else ids
    activated = ids[:count] if phase == "publishing" else (ids if phase == "activated" else [])
    if count and phase in {"displacing", "displaced", "publishing", "activated"}:
        os.replace(existing, rollbacks[0])
        if phase in {"publishing", "activated"}:
            existing.mkdir()
            (existing / "new").write_text("new")
        else:
            existing = profile.paths.mutable_root
    journal = profile.paths.backup_root / ".restore-journal-crash.json"
    backups._write_json_fsync(journal, {"phase": phase, "backup_id": "crash", "displaced": list(displaced), "activated": list(activated), "roots": [
        {"root_id": rid, "destination": str(root), "staging": str(staging), "rollback": str(rollback), "original_exists": i == 0}
        for i, (rid, root, staging, rollback) in enumerate(zip(ids, roots, stagings, rollbacks))
    ]})
    RestoreService(profile, backup_service=_NoopBackup(), stopped_check=lambda: True).reconcile()
    assert (profile.paths.mutable_root / "old").read_text() == "old"
    assert not second.exists()
    assert not list(profile.paths.backup_root.glob(".restore-journal-*.json"))
    assert not any(path.exists() for path in (*stagings, *rollbacks))


class _NoopBackup:
    def create(self, **_kwargs):
        return None


class _RecordingBackup:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return None
