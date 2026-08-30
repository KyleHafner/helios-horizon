from pathlib import Path
import tomllib

import pytest

from game_control.models import (
    AdapterKind,
    CuratedModpackSpec,
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


TMODLOADER_145_MIGRATION_DIR = (
    Path(__file__).parents[1] / "ops" / "migrations" / "tmodloader-145"
)


MINECRAFT = """\
id = "minecraft"
display_name = "Minecraft"
adapter = "crafty"
crafty_server_id = "00000000-0000-4000-8000-000000000000"
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

SYSTEMD = MINECRAFT.replace(
    'id = "minecraft"', 'id = "terraria-vanilla"'
).replace(
    'adapter = "crafty"\ncrafty_server_id = "00000000-0000-4000-8000-000000000000"',
    'adapter = "systemd"\nsystemd_unit = "terraria-vanilla.service"',
)


def test_registry_accepts_only_fixed_ids_and_units(tmp_path: Path):
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)
    assert registry.require("minecraft").display_name == "Minecraft"
    with pytest.raises(KeyError):
        registry.require("minecraft;systemctl stop crafty")


def test_candidate_toml_loads_through_profile_id_registry():
    registry = ProfileRegistry.load(TMODLOADER_145_MIGRATION_DIR)

    candidate = registry.require("terraria-tmod-145-candidate")

    assert isinstance(candidate.id, ProfileId)
    assert candidate.id.value == "terraria-tmod-145-candidate"


def test_sunlit_gc_layer_is_separate_from_modpack_argument_files():
    root = Path(__file__).parents[1]
    dropin = (root / "ops/systemd/minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf").read_text()
    assert "JAVA_TOOL_OPTIONS=-Xlog:gc*" in dropin
    assert "user_jvm_args.txt" not in dropin
    assert "unix_args.txt" not in dropin


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
        crafty_server_id="00000000-0000-4000-8000-000000000000",
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


def test_release_update_accepts_strict_optional_sha256_and_closed_models():
    digest = "a" * 64
    spec = UpdateSpec(
        kind="release_symlink",
        download_url="https://example.test/game.tar.gz",
        executable_relative_path="bin/game",
        sha256=digest,
    )
    assert spec.sha256 == digest
    with pytest.raises(ValueError):
        UpdateSpec(
            kind="release_symlink",
            download_url="https://example.test/game.tar.gz",
            executable_relative_path="bin/game",
            sha256="g" * 64,
        )
    with pytest.raises(ValueError):
        UpdateSpec(
            kind="release_symlink",
            download_url="https://example.test/game.tar.gz",
            executable_relative_path="bin/game",
            sha256="a" * 63,
        )
    with pytest.raises(ValueError):
        UpdateSpec.model_validate({"kind": "manual", "sha256": digest})
    with pytest.raises(ValueError):
        UpdateSpec.model_validate({"kind": "steamcmd_in_place", "app_id": 123, "sha256": digest})
    with pytest.raises(ValueError):
        UpdateSpec.model_validate({"kind": "release_symlink", "sha256": digest, "unexpected": True})


def test_curated_modpack_requires_exact_identity_and_separate_layout(tmp_path: Path):
    digest = "b" * 64
    curated = CuratedModpackSpec(
        version="1.1.2-SSV4.1.4",
        project_id=1495800,
        file_id=8717959,
        size_bytes=687280069,
        manifest_path="/etc/game-control/update-manifests/sunlit-1.1.2.json",
        release_root="/opt/game-servers/minecraft-sunlit-cobblemon/releases",
        state_root="/srv/game-servers/minecraft-sunlit-cobblemon-state",
        active_link="/srv/game-servers/minecraft-sunlit-cobblemon-current",
    )
    spec = UpdateSpec(
        kind="curated_modpack",
        download_url="https://example.test/server-pack.zip",
        sha256=digest,
        curated=curated,
    )
    assert spec.curated == curated

    with pytest.raises(ValueError, match="fixed artifact"):
        UpdateSpec(kind="curated_modpack", download_url="https://example.test/server-pack.zip")
    with pytest.raises(ValueError, match="must not overlap"):
        CuratedModpackSpec.model_validate(
            {
                **curated.model_dump(),
                "state_root": "/opt/game-servers/minecraft-sunlit-cobblemon/releases/state",
            }
        )


def test_release_profile_requires_trusted_digest_for_update_apply():
    root = Path(__file__).parents[1]
    vanilla = tomllib.loads((root / "config/profiles/terraria-vanilla.toml").read_text())
    vanilla["operations"].append("update_apply")
    with pytest.raises(ValueError, match="trusted sha256"):
        Profile.model_validate(vanilla)

    tmod = Profile.model_validate(
        tomllib.loads((root / "config/profiles/terraria-tmod.toml").read_text())
    )
    assert OperationName.UPDATE_APPLY in tmod.operations
    assert tmod.update.sha256 is not None


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


def test_registry_rejects_missing_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="profile directory does not exist"):
        ProfileRegistry.load(tmp_path / "missing")


@pytest.mark.parametrize(
    ("profile", "field"),
    [
        (MINECRAFT.replace('display_name = "Minecraft"', 'display_name = ""'), "display_name"),
        (MINECRAFT.replace('adapter = "crafty"', 'adapter = "unknown"'), "adapter"),
        (MINECRAFT.replace('display_name = "Minecraft"\n', ""), "display_name"),
    ],
)
def test_registry_rejects_missing_and_invalid_profile_fields(
    tmp_path: Path, profile: str, field: str
):
    (tmp_path / "minecraft.toml").write_text(profile)

    with pytest.raises(ValueError, match=field):
        ProfileRegistry.load(tmp_path)


def test_registry_rejects_toml_parse_errors(tmp_path: Path):
    (tmp_path / "minecraft.toml").write_text('id = "minecraft"\nports = [')

    with pytest.raises(ValueError, match="Invalid value"):
        ProfileRegistry.load(tmp_path)


def test_registry_rejects_a_path_outside_declared_data_root(tmp_path: Path):
    profile = MINECRAFT.replace(
        'install_root = "/srv/test/minecraft"',
        'install_root = "/srv/test/other"',
    )
    (tmp_path / "minecraft.toml").write_text(profile)

    with pytest.raises(ValueError, match="outside data root"):
        ProfileRegistry.load(tmp_path)


def test_registry_rejects_a_path_that_escapes_through_a_symlink(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = data_root / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    profile = MINECRAFT.replace("/srv/test/minecraft", str(data_root)).replace(
        f'mutable_root = "{data_root}"',
        f'mutable_root = "{linked / "mutable"}"',
    )
    (tmp_path / "minecraft.toml").write_text(profile)

    with pytest.raises(ValueError, match="escapes data root through symlink"):
        ProfileRegistry.load(tmp_path)


def test_registry_rejects_systemd_units_with_parent_segments(tmp_path: Path):
    profile = SYSTEMD.replace(
        'systemd_unit = "terraria-vanilla.service"',
        'systemd_unit = "terraria..service"',
    )
    (tmp_path / "terraria-vanilla.toml").write_text(profile)

    with pytest.raises(ValueError, match="invalid systemd unit"):
        ProfileRegistry.load(tmp_path)


def test_registry_loads_profiles_in_filename_order_and_applies_defaults(tmp_path: Path):
    systemd_defaults = SYSTEMD.replace(
        'notification_events = ["start", "stop", "failed_start", "crash"]\n', ""
    )
    (tmp_path / "terraria-vanilla.toml").write_text(systemd_defaults)
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)

    registry = ProfileRegistry.load(tmp_path)

    assert [profile.id.value for profile in registry] == ["minecraft", "terraria-vanilla"]
    assert registry.profiles == tuple(registry)
    assert len(registry) == 2
    assert registry.require(ProfileId.MINECRAFT).idle_stop_minutes == 0
    assert registry.require("terraria-vanilla").notification_events == frozenset()


@pytest.mark.parametrize("minutes", [-1, 1, 1441, "15"])
def test_update_idle_stop_rejects_out_of_range_or_non_integer_values(
    tmp_path: Path, minutes: object
):
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)

    with pytest.raises(ValueError, match="idle_stop_minutes"):
        registry.update_idle_stop("minecraft", minutes)  # type: ignore[arg-type]


def test_update_idle_stop_inserts_root_value_and_refreshes_registry(tmp_path: Path):
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)

    updated = registry.update_idle_stop(ProfileId.MINECRAFT, 15)
    raw = (tmp_path / "minecraft.toml").read_text()

    assert updated.idle_stop_minutes == 15
    assert registry.require("minecraft").idle_stop_minutes == 15
    assert raw.count("idle_stop_minutes = 15") == 1
    assert raw.index("idle_stop_minutes = 15") < raw.index("[process]")


def test_update_idle_stop_replaces_existing_root_value(tmp_path: Path):
    profile = MINECRAFT.replace(
        '\n\n[process]', '\nidle_stop_minutes = 10\n\n[process]'
    )
    (tmp_path / "minecraft.toml").write_text(profile)
    registry = ProfileRegistry.load(tmp_path)

    registry.update_idle_stop("minecraft", 30)
    raw = (tmp_path / "minecraft.toml").read_text()

    assert "idle_stop_minutes = 10" not in raw
    assert raw.count("idle_stop_minutes = 30") == 1


def test_update_idle_stop_fails_when_registry_is_not_writable(tmp_path: Path):
    (tmp_path / "minecraft.toml").write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)
    registry._root = None

    with pytest.raises(RuntimeError, match="not writable"):
        registry.update_idle_stop("minecraft", 15)


@pytest.mark.parametrize("replacement", ["remove", "symlink"])
def test_update_idle_stop_fails_when_profile_configuration_is_unavailable(
    tmp_path: Path, replacement: str
):
    profile_path = tmp_path / "minecraft.toml"
    profile_path.write_text(MINECRAFT)
    registry = ProfileRegistry.load(tmp_path)
    profile_path.unlink()
    if replacement == "symlink":
        target = tmp_path / "target.toml"
        target.write_text(MINECRAFT)
        profile_path.symlink_to(target)

    with pytest.raises(RuntimeError, match="configuration is unavailable"):
        registry.update_idle_stop("minecraft", 15)
