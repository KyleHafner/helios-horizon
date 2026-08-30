from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from game_control.api import BackupBody
from game_control.backups import (
    B2CommandTransport,
    B2ProtectionService,
    BackupClass,
    BackupRecord,
    RemoteObject,
    b2_prefix,
    full_lxc_prefix,
    validate_destination,
)
from game_control.errors import SafeError
from game_control.models import BackupDestination


class _Transport:
    def __init__(self):
        self.objects: dict[str, Path] = {}
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.fail_upload = False
        self.fail_verify = False
        self.fail_delete = False

    def upload(self, source: Path, key: str) -> None:
        if self.fail_upload:
            raise RuntimeError("credential=do-not-leak")
        self.objects[key] = source

    def verify(self, source: Path, key: str) -> None:
        if self.fail_verify:
            raise RuntimeError("token=do-not-leak")
        assert self.objects[key].read_bytes() == source.read_bytes()

    def list(self, prefix: str):
        self.listed.append(prefix)
        return tuple(RemoteObject(key) for key in self.objects)

    def delete(self, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("secret=do-not-leak")
        self.deleted.append(key)
        self.objects.pop(key, None)


class _Db:
    def __init__(self):
        self.rows: list[dict] = []

    def list_backups(self, profile_id):
        return [row for row in self.rows if row["profile_id"] == profile_id]


def _record(tmp_path: Path, backup_id: str, *, protected: bool = False) -> BackupRecord:
    archive = tmp_path / f"{backup_id}.tar.zst"
    archive.write_bytes(backup_id.encode())
    return BackupRecord(
        id=backup_id,
        profile_id="minecraft-sunlit-cobblemon",
        created_at=datetime.strptime(backup_id.split("Z", 1)[0], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc),
        size_bytes=archive.stat().st_size,
        verified=True,
        protected=protected,
        path=archive,
    )


def test_destination_validation_is_fixed_and_allowlisted():
    assert validate_destination(BackupDestination.LOCAL, "pz-rising") is BackupDestination.LOCAL
    assert b2_prefix("minecraft-sunlit-cobblemon") == "helios/horizon/app/minecraft-sunlit-cobblemon"
    assert full_lxc_prefix() == "helios/horizon/full-lxc"
    with pytest.raises(SafeError, match="not approved"):
        validate_destination("horizon-b2", "pz-rising")
    with pytest.raises(SafeError, match="not approved"):
        b2_prefix("minecraft")
    with pytest.raises(SafeError, match="not approved"):
        b2_prefix("minecraft", "full-vm")
    with pytest.raises(SafeError, match="not approved"):
        b2_prefix("minecraft-sunlit-cobblemon", BackupClass.FULL_LXC)
    with pytest.raises(ValidationError):
        BackupBody.model_validate({"destination": "arbitrary"})


def test_b2_argv_has_fixed_remote_cryptcheck_and_no_shell_or_credentials(tmp_path: Path, monkeypatch):
    calls = []
    archive = tmp_path / "20260805T000000000000Z-0123456789ab.tar.zst"
    archive.write_bytes(b"archive")

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr("game_control.interim_maintenance_control.active_block_schedulers", lambda: ("none",))
    transport = B2CommandTransport(runner=runner, credential_validator=lambda: None)
    key = "helios/horizon/app/minecraft-sunlit-cobblemon/20260805T000000000000Z-0123456789ab.tar.zst"
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    staged = stage_dir / archive.name
    transport.upload(archive, key)
    transport.verify(archive, key)
    transport.download(key, staged)
    expected = [
        ["copyto", str(archive), "helios-b2-crypt:" + key, "--immutable"],
        ["cryptcheck", str(tmp_path), "helios-b2-crypt:helios/horizon/app/minecraft-sunlit-cobblemon",
         "--one-way", "--fast-list", "--include", archive.name],
        ["copyto", "helios-b2-crypt:" + key, str(staged), "--immutable"],
    ]
    for call, inner in zip(calls, expected):
        argv = call[0]
        assert argv[:7] == ["/usr/bin/systemd-run", "--wait", "--pipe", "--quiet",
                            "--service-type=exec", "--slice=maintenance.slice", argv[6]]
        separator = argv.index("--")
        assert argv[separator + 1:] == ["/usr/bin/nice", "-n", "10", "/usr/bin/rclone",
                                        "--bwlimit", "8M", "--config", "/etc/game-control/secrets.d/horizon-b2-rclone.conf", *inner]
        assert any(item.startswith("--unit=horizon-maint-") for item in argv)
    assert all(call[1]["shell"] is False for call in calls)
    assert all("do-not-leak" not in repr(call) for call in calls)


def test_b2_operation_revalidates_fixed_credentials_before_runner(tmp_path: Path):
    calls = []
    archive = tmp_path / "20260805T000000000000Z-0123456789ab.tar.zst"
    archive.write_bytes(b"archive")
    key = (
        "helios/horizon/app/minecraft-sunlit-cobblemon/"
        "20260805T000000000000Z-0123456789ab.tar.zst"
    )

    def reject_credentials() -> None:
        raise SafeError(
            "backup_protection_failed", "remote backup credentials are unavailable"
        )

    transport = B2CommandTransport(
        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        credential_validator=reject_credentials,
    )
    with pytest.raises(SafeError, match="credentials are unavailable"):
        transport.upload(archive, key)
    assert calls == []


def test_success_records_remote_verification_and_retains_exactly_two(tmp_path: Path):
    transport = _Transport()
    database = _Db()
    service = B2ProtectionService(database=database, transport=transport)
    records = []
    for index in range(3):
        record = _record(tmp_path, f"20260805T00000{index}000000Z-{index}")
        database.rows.append({"id": record.id, "profile_id": record.profile_id, "created_at": record.created_at,
                              "size_bytes": record.size_bytes, "verified": True, "protected": False})
        records.append(record)
        result = service.protect(record)
        assert result.local_verified is True
        assert result.remote_verified is True
        assert result.comparison_state == "verified"
    prefix = b2_prefix("minecraft-sunlit-cobblemon")
    assert set(transport.objects) == {
        f"{prefix}/{records[1].id}.tar.zst",
        f"{prefix}/{records[2].id}.tar.zst",
    }
    assert len(transport.deleted) == 1


def test_unrelated_objects_and_protected_local_backup_are_immune(tmp_path: Path):
    transport = _Transport()
    prefix = b2_prefix("minecraft-sunlit-cobblemon")
    transport.objects[f"{prefix}/untracked.tar.zst"] = tmp_path / "untracked"
    transport.objects["other-application/minecraft/keep.tar.zst"] = tmp_path / "keep"
    database = _Db()
    service = B2ProtectionService(database=database, transport=transport)
    old = _record(tmp_path, "20260805T000000000000Z-aaaaaaaaaaaa", protected=True)
    middle = _record(tmp_path, "20260805T000001000000Z-bbbbbbbbbbbb")
    current = _record(tmp_path, "20260805T000002000000Z-cccccccccccc")
    for record in (old, middle, current):
        database.rows.append({"id": record.id, "profile_id": record.profile_id, "created_at": record.created_at,
                              "size_bytes": record.size_bytes, "verified": True, "protected": record.protected})
        service.protect(record)
    assert f"{prefix}/{old.id}.tar.zst" not in transport.objects
    assert f"{prefix}/{middle.id}.tar.zst" in transport.objects
    assert f"{prefix}/{current.id}.tar.zst" in transport.objects
    assert f"{prefix}/untracked.tar.zst" in transport.objects
    assert "other-application/minecraft/keep.tar.zst" in transport.objects
    assert transport.deleted == [f"{prefix}/{old.id}.tar.zst"]


def test_transport_rejects_arbitrary_keys_and_prefixes_before_runner_calls(tmp_path: Path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]", stderr=b"")

    transport = B2CommandTransport(runner=runner, credential_validator=lambda: None)
    archive = tmp_path / "20260805T000000000000Z-0123456789ab.tar.zst"
    archive.write_bytes(b"archive")
    invalid_key = "helios/horizon/app/minecraft-sunlit-cobblemon/not-generated.tar.zst"
    with pytest.raises(SafeError):
        transport.upload(archive, invalid_key)
    with pytest.raises(SafeError):
        transport.verify(archive, invalid_key)
    with pytest.raises(SafeError):
        transport.delete(invalid_key)
    with pytest.raises(SafeError):
        transport.list("helios/horizon/full-lxc")
    with pytest.raises(SafeError):
        transport.list("helios/horizon/app/minecraft-sunlit-cobblemon/other")
    assert calls == []


def test_application_retention_cannot_list_or_delete_full_lxc_objects(tmp_path: Path):
    transport = _Transport()
    transport.objects[full_lxc_prefix() + "/20260805T000000000000Z-0123456789ab.tar.zst"] = tmp_path / "full"
    database = _Db()
    service = B2ProtectionService(database=database, transport=transport)
    record = _record(tmp_path, "20260805T000001000000Z-0123456789ab")
    database.rows.append({
        "id": record.id,
        "profile_id": record.profile_id,
        "created_at": record.created_at,
        "size_bytes": record.size_bytes,
        "verified": True,
        "protected": False,
    })
    service.protect(record)
    assert transport.listed == [b2_prefix(record.profile_id)]
    assert full_lxc_prefix() + "/20260805T000000000000Z-0123456789ab.tar.zst" in transport.objects


@pytest.mark.parametrize("failure", ["upload", "verify", "delete"])
def test_failures_never_remove_local_verified_archive(tmp_path: Path, failure: str):
    transport = _Transport()
    setattr(transport, f"fail_{failure}", True)
    service = B2ProtectionService(database=_Db(), transport=transport)
    record = _record(tmp_path, "20260805T000002000000Z-fail")
    if failure == "delete":
        for index in range(2):
            old = _record(tmp_path, f"20260805T00000{index}000000Z-old{index}")
            service.protect(old)
    with pytest.raises(SafeError, match="remote backup"):
        service.protect(record)
    assert record.path.exists()


def test_command_failure_is_redacted_from_public_error(tmp_path: Path):
    def runner(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["rclone", "token=do-not-leak"], stderr=b"secret=do-not-leak")

    archive = tmp_path / "20260805T000000000000Z-0123456789ab.tar.zst"
    archive.write_bytes(b"archive")
    key = "helios/horizon/app/minecraft-sunlit-cobblemon/20260805T000000000000Z-0123456789ab.tar.zst"
    with pytest.raises(SafeError) as error:
        B2CommandTransport(runner=runner, credential_validator=lambda: None).upload(archive, key)
    assert "do-not-leak" not in str(error.value)
