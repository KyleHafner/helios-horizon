from __future__ import annotations

import hashlib
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
from game_control.worlds import WorldService


def _profile(tmp_path: Path, profile_id: ProfileId, port: int) -> Profile:
    data, backup = tmp_path / profile_id.value, tmp_path / f"{profile_id.value}-backups"
    data.mkdir()
    return Profile(
        id=profile_id,
        display_name=profile_id.value,
        adapter=AdapterKind.SYSTEMD,
        systemd_unit=f"{profile_id.value}.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=port),),
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
        operations=frozenset({OperationName.BACKUP, OperationName.CLONE_SOURCE, OperationName.CLONE_TARGET}),
        update=UpdateSpec(kind="manual"),
    )


def test_clone_never_mutates_or_overwrites_source(tmp_path: Path):
    vanilla = _profile(tmp_path, ProfileId.TERRARIA_VANILLA, 7777)
    tmod = _profile(tmp_path, ProfileId.TERRARIA_TMOD, 7778)
    source = vanilla.paths.mutable_root / "swag.wld"
    source.write_bytes(b"world")
    before = hashlib.sha256(source.read_bytes()).digest()
    service = WorldService(
        vanilla,
        tmod,
        backup_service=BackupService(vanilla, stopped_check=lambda: True),
        stopped_check=lambda: True,
    )

    result = service.clone_vanilla_to_tmod("swag.wld", "swag-modded")

    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert result.destination.name.startswith("swag-modded-")
    assert result.destination.read_bytes() == b"world"
    with pytest.raises(SafeError, match="already exists"):
        service.clone_to_exact_existing_destination("swag.wld", result.destination)
