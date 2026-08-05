from pathlib import Path

import pytest

from game_control.models import (
    AdapterKind,
    OperationName,
    PathSpec,
    PortSpec,
    Profile,
    ProfileId,
    ProcessSpec,
    UpdateSpec,
)
from game_control.profile import ProfileRegistry
from game_control.service_wiring import _ProfilesFacade


MINECRAFT = """\
id = "minecraft"
display_name = "Minecraft"
adapter = "crafty"
crafty_server_id = "00000000-0000-4000-8000-000000000001"
ports = [{protocol = "tcp", port = 25565}]
start_timeout_seconds = 180
stop_timeout_seconds = 90
min_available_memory_bytes = 7516192768
min_free_disk_bytes = 8589934592
health_timeout_seconds = 180
operations = ["start", "stop", "restart", "force_stop", "backup", "restore", "update_check"]
notification_events = ["start", "stop", "failed_start", "crash"]

[process]
executable = "/usr/bin/java"
argv_contains = ["forge"]

[paths]
data_roots = ["/srv/test/minecraft"]
mutable_root = "/srv/test/minecraft"
log_files = ["/srv/test/minecraft/logs/latest.log"]
backup_root = "/var/backups/game-servers/minecraft"
install_root = "/srv/test/minecraft"
version_file = "/srv/test/minecraft/variables.txt"

[update]
kind = "manual"
"""


def test_registry_accepts_only_fixed_ids_and_units(tmp_path: Path):
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)
    assert registry.require("minecraft").display_name == "Minecraft"
    with pytest.raises(KeyError):
        registry.require("minecraft;systemctl stop crafty")


def test_systemd_profile_rejects_non_allowlisted_unit(tmp_path: Path):
    (tmp_path / "bad.toml").write_text(
        """\
id="bad"
display_name="Bad"
adapter="systemd"
systemd_unit="../../ssh.service"
ports=[{protocol="tcp", port=25565}]
start_timeout_seconds=30
stop_timeout_seconds=30
min_available_memory_bytes=1
min_free_disk_bytes=1
health_timeout_seconds=30
operations=["start"]
notification_events=[]

[process]
executable="/bin/false"
argv_contains=[]

[paths]
data_roots=["/srv/test/bad"]
mutable_root="/srv/test/bad"
log_files=[]
backup_root="/var/backups/game-servers/bad"
install_root="/srv/test/bad"
version_file="/srv/test/bad/version"

[update]
kind="manual"
"""
    )
    with pytest.raises(ValueError, match="systemd unit"):
        ProfileRegistry.load(tmp_path)


def test_registry_rejects_duplicate_ports_and_filename_mismatch(tmp_path: Path):
    duplicate = MINECRAFT.replace(
        'ports = [{protocol = "tcp", port = 25565}]',
        'ports = [{protocol = "tcp", port = 25565}, {protocol = "tcp", port = 25565}]',
    )
    (tmp_path / "minecraft.toml").write_text(duplicate)
    with pytest.raises(ValueError, match="duplicate port"):
        ProfileRegistry.load(tmp_path)

    (tmp_path / "other.toml").write_text(MINECRAFT)
    (tmp_path / "minecraft.toml").unlink()
    with pytest.raises(ValueError, match="filename"):
        ProfileRegistry.load(tmp_path)


def test_models_are_closed_and_frozen():
    process = ProcessSpec(executable="/usr/bin/java")
    paths = PathSpec(
        data_roots=("/srv/game",),
        mutable_root="/srv/game",
        backup_root="/var/backups/game",
        install_root="/srv/game",
        version_file="/srv/game/version",
    )
    profile = Profile(
        id=ProfileId.MINECRAFT,
        display_name="Minecraft",
        adapter=AdapterKind.CRAFTY,
        crafty_server_id="00000000-0000-4000-8000-000000000001",
        process=process,
        ports=(PortSpec(protocol="tcp", port=25565),),
        start_timeout_seconds=30,
        stop_timeout_seconds=30,
        health_timeout_seconds=30,
        paths=paths,
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.START}),
        update=UpdateSpec(kind="manual"),
    )
    with pytest.raises(Exception):
        profile.display_name = "changed"
    with pytest.raises(Exception):
        Profile.model_validate({**profile.model_dump(), "unexpected": True})


def test_profile_path_and_update_validation():
    with pytest.raises(ValueError):
        ProcessSpec(executable="/usr/bin/../bin/java")
    with pytest.raises(ValueError):
        PathSpec(
            data_roots=("/srv/game",),
            mutable_root="/srv/game",
            backup_root="/srv/game",
            install_root="/srv/game",
            version_file="/srv/game/version",
        )
    with pytest.raises(ValueError):
        UpdateSpec(kind="manual", app_id=123)


def test_public_profile_exposes_adapter_kind_for_dashboard_family_derivation():
    profile = type(
        "ProfileView",
        (),
        {
            "id": ProfileId.MINECRAFT,
            "display_name": "Minecraft",
            "adapter": AdapterKind.CRAFTY,
            "operations": frozenset({OperationName.START}),
            "public_endpoint": None,
        },
    )()

    public = _ProfilesFacade({"minecraft": profile}).public_profiles()[0]

    assert public.adapter is AdapterKind.CRAFTY


def test_registry_rejects_symlinked_backup_root_inside_data_root(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    backup_link = tmp_path / "backup"
    backup_link.symlink_to(data_root, target_is_directory=True)
    profile = MINECRAFT.replace("/srv/test/minecraft", str(data_root)).replace(
        "/var/backups/game-servers/minecraft", str(backup_link)
    )
    (tmp_path / "minecraft.toml").write_text(profile)
    with pytest.raises(ValueError, match="backup_root"):
        ProfileRegistry.load(tmp_path)
