from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from game_control.models import (
    AdapterKind,
    NotificationEvent,
    OperationName,
    Profile,
)
from game_control.managed_tuning import validate_slice_policy


ROOT = Path(__file__).parents[1]
PROFILE_DIR = ROOT / "config" / "profiles"
RUNNER_DIR = ROOT / "config" / "runner"
UNIT_DIR = ROOT / "ops" / "systemd"
NFTABLES = ROOT / "ops" / "nftables" / "horizon.nft"
TMODLOADER_145_MIGRATION_DIR = ROOT / "ops" / "migrations" / "tmodloader-145"
ACTIVE_PROFILE_IDS = (
    "minecraft-sunlit-cobblemon",
    "terraria-vanilla",
    "terraria-tmod",
)


def test_readme_ci_badge_targets_public_repository_owner() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = "https://github.com/swagsystems/helios-horizon/actions/workflows/ci.yml"
    assert readme.count(expected) == 2
    assert "KyleHafner/helios-horizon" not in readme


def _profiles() -> dict[str, Profile]:
    return {
        path.stem: Profile.model_validate(tomllib.loads(path.read_text()))
        for path in sorted(PROFILE_DIR / f"{profile_id}.toml" for profile_id in ACTIVE_PROFILE_IDS)
    }


def _unit(name: str) -> str:
    return (UNIT_DIR / name).read_text()


def _path_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(path for item in value for path in _path_strings(item))
    if isinstance(value, dict):
        return tuple(path for item in value.values() for path in _path_strings(item))
    return ()


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _directive(unit: str, name: str) -> str:
    lines = [line for line in unit.splitlines() if line.startswith(f"{name}=")]
    assert len(lines) == 1
    return lines[0].split("=", 1)[1]


def test_all_closed_profiles_match_production_contract() -> None:
    profiles = _profiles()
    assert set(profiles) == {
        "minecraft-sunlit-cobblemon",
        "terraria-vanilla",
        "terraria-tmod",
    }
    sunlit = profiles["minecraft-sunlit-cobblemon"]
    assert sunlit.adapter is AdapterKind.SYSTEMD
    assert sunlit.systemd_unit == "minecraft-sunlit-cobblemon.service"
    assert sunlit.crafty_server_id is None
    assert sunlit.public_endpoint is not None
    assert sunlit.public_endpoint.host == "mc.example.com"
    assert sunlit.public_endpoint.port == 25565
    assert sunlit.public_endpoint.relay_unit == "bore-minecraft-fenced.service"
    assert profiles["minecraft-sunlit-cobblemon"].ports[0].port == 25566
    assert sunlit.paths.data_roots == (
        Path("/srv/game-servers/minecraft-sunlit-cobblemon-state"),
        Path("/opt/game-servers/minecraft-sunlit-cobblemon/releases"),
        Path("/srv/game-servers/minecraft-sunlit-cobblemon-current"),
    )
    assert sunlit.paths.backup_roots == (
        Path("/srv/game-servers/minecraft-sunlit-cobblemon-state"),
    )
    assert sunlit.paths.mutable_root == Path("/srv/game-servers/minecraft-sunlit-cobblemon-state")
    assert sunlit.paths.install_root == Path("/opt/game-servers/minecraft-sunlit-cobblemon/releases")
    assert sunlit.paths.backup_root == Path("/var/backups/game-servers/minecraft-sunlit-cobblemon")
    assert OperationName.COMMAND in sunlit.operations
    assert OperationName.BENCHMARK in sunlit.operations
    assert sunlit.process.argv_contains == (
        "@/opt/game-servers/minecraft-sunlit-cobblemon/libraries/net/minecraftforge/forge/1.20.1-47.4.0/unix_args.txt",
    )
    for profile_id in ("terraria-vanilla", "terraria-tmod"):
        profile = profiles[profile_id]
        assert profile.adapter is AdapterKind.SYSTEMD
        assert profile.crafty_server_id is None
        assert profile.systemd_unit == f"{profile_id}.service"
        assert {(p.protocol, p.port) for p in profile.ports} == {("tcp", 7777)}
        assert profile.update.kind == "release_symlink"
        assert profile.public_endpoint is not None
        assert profile.public_endpoint.host == "terraria.example.com"
        assert profile.public_endpoint.relay_unit == "horizon-terraria-relay.service"
        assert profile.paths.data_roots == (
            profile.paths.mutable_root,
            profile.paths.install_root,
        )
        assert profile.paths.backup_roots == (profile.paths.mutable_root,)
    assert OperationName.CLONE_SOURCE in profiles["terraria-vanilla"].operations
    assert OperationName.CLONE_TARGET not in profiles["terraria-vanilla"].operations
    assert OperationName.UPDATE_CHECK in profiles["terraria-vanilla"].operations
    assert OperationName.UPDATE_APPLY not in profiles["terraria-vanilla"].operations
    assert OperationName.UPDATE_APPLY in profiles["terraria-tmod"].operations
    assert profiles["terraria-tmod"].update.sha256 is not None


def test_target_firewall_is_fixed_private_and_fail_closed() -> None:
    policy = NFTABLES.read_text()
    assert "flush ruleset" in policy
    assert policy.count("policy drop") == 2
    assert "policy accept" in policy
    assert "elements = { 192.0.2.10 }" in policy
    assert "elements = { 192.0.2.11 }" in policy
    assert "elements = { 192.0.2.12 }" in policy
    assert "ip saddr @npm_v4 tcp dport 8444 accept" in policy
    assert "ip saddr @monitoring_v4 tcp dport 8444 accept" in policy
    assert "ip saddr @admin_v4 tcp dport { 25565, 7777 } accept" in policy
    assert (
        'iifname "wg-hzn-terraria" ip saddr 192.0.2.13 '
        "tcp dport 7777 accept"
    ) in policy
    assert "tcp dport 25575 drop" in policy
    assert "tcp dport 25575 accept" not in policy


def test_legacy_source_history_is_not_a_target_profile_set() -> None:
    assert (PROFILE_DIR / "minecraft.toml").is_file()
    assert (PROFILE_DIR / "pz-rising.toml").is_file()
    assert set(path.stem for path in PROFILE_DIR.glob("*.toml")) >= {
        "minecraft",
        "pz-rising",
    }


def test_runner_configs_are_fixed_and_secret_free() -> None:
    expected = {
        "minecraft-sunlit-cobblemon": {
            "user": "svc-sunlit",
            "cwd": "/srv/game-servers/minecraft-sunlit-cobblemon-current",
            "argv": [
                "/usr/bin/java",
                "-Duser.home=/srv/game-servers/minecraft-sunlit-cobblemon-state/local",
                "@/srv/game-servers/minecraft-sunlit-cobblemon-current/user_jvm_args.txt",
                "@/opt/game-servers/minecraft-sunlit-cobblemon/libraries/net/minecraftforge/forge/1.20.1-47.4.0/unix_args.txt",
                "nogui",
            ],
            "environment": {
                "HOME": "/srv/game-servers/minecraft-sunlit-cobblemon-state/local",
            },
        },
        "terraria-vanilla": {
            "user": "terraria-vanilla",
            "cwd": "/srv/game-servers/terraria-vanilla",
            "argv": [
                "/opt/game-servers/terraria-vanilla/current/TerrariaServer.bin.x86_64",
                "-config",
                "/srv/game-servers/terraria-vanilla/config/serverconfig.txt",
            ],
        },
        "terraria-tmod": {
            "user": "tmodloader",
            "cwd": "/opt/game-servers/terraria-tmod/current",
            "argv": [
                "/opt/game-servers/terraria-tmod/current/dotnet/dotnet",
                "/opt/game-servers/terraria-tmod/current/tModLoader.dll",
                "-server",
                "-config",
                "/srv/game-servers/terraria-tmod/config/serverconfig.txt",
                "-tmlsavedirectory",
                "/srv/game-servers/terraria-tmod",
            ],
            "environment": {
                "HOME": "/srv/game-servers/terraria-tmod",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1",
                "DOTNET_ROLL_FORWARD": "Disable",
            },
        },
    }
    for profile_id, contract in expected.items():
        config = json.loads((RUNNER_DIR / f"{profile_id}.json").read_text())
        expected_keys = {"user", "group", "cwd", "argv", "environment"}
        if profile_id.startswith("terraria-"):
            expected_keys.add("console_fifo")
        assert set(config) == expected_keys
        assert config["user"] == contract["user"]
        assert config["group"] == contract["user"]
        assert config["cwd"] == contract["cwd"]
        if "argv" in contract:
            assert config["argv"] == contract["argv"]
        assert config["environment"] == contract.get("environment", {})
        if profile_id.startswith("terraria-"):
            assert config["console_fifo"] == f"/run/game-slot/{profile_id}.console"
        assert not re.search(r"\$\{|\$\(|`|;\s*(?:sh|bash)", json.dumps(config))
    runner = (ROOT / "ops/bin/game-slot-run").read_text()
    assert "shell executables are not permitted" in runner
    assert "value[0] != \"/usr/bin/bash\"" in runner
    assert (RUNNER_DIR / "minecraft.json").is_file()
    assert (RUNNER_DIR / "pz-rising.json").is_file()

    revoke_all = (ROOT / "ops/bin/horizon-session-revoke-all").read_text()
    assert "SessionStore.open()" in revoke_all
    assert "/var/lib/game-control/session-revoke-all.jsonl" in revoke_all
    assert "sys.argv != [sys.argv[0]]" in revoke_all


def test_stable_tmodloader_invokes_pinned_dotnet_without_mutating_release() -> None:
    runner = json.loads((RUNNER_DIR / "terraria-tmod.json").read_text())
    profile = tomllib.loads((PROFILE_DIR / "terraria-tmod.toml").read_text())
    runtime_root = "/opt/game-servers/terraria-tmod/current"

    assert runner["cwd"] == runtime_root
    assert runner["argv"][:3] == [
        f"{runtime_root}/dotnet/dotnet",
        f"{runtime_root}/tModLoader.dll",
        "-server",
    ]
    assert not any("start-tModLoaderServer.sh" in value for value in runner["argv"])
    assert runner["environment"] == {
        "HOME": "/srv/game-servers/terraria-tmod",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_ROLL_FORWARD": "Disable",
    }
    assert runner["argv"] == [
        profile["process"]["executable"],
        *profile["process"]["argv_contains"],
    ]
    unit = _unit("terraria-tmod.service")
    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    assert runtime_root not in writable
    log_source = "/srv/game-servers/terraria-tmod/logs/tModLoader-Logs"
    log_target = f"{runtime_root}/tModLoader-Logs"
    assert f"AssertPathIsDirectory={log_source}" in unit
    assert f"AssertPathIsDirectory={log_target}" in unit
    assert f"BindPaths={log_source}:{log_target}" in unit


def test_units_hardened_and_heavy_services_do_not_autostart() -> None:
    slotd = _unit("game-slotd.service")
    web = _unit("game-control-web.service")
    assert "User=root" in slotd
    assert "Slice=horizon.slice" in slotd
    assert "OnFailure=horizon-alert-notify@controller.service" in slotd
    assert "LogNamespace=horizon" in slotd
    for directive in ("PrivateTmp=yes", "ProtectHome=read-only", "ProtectSystem=strict", "RestrictSUIDSGID=yes"):
        assert directive in slotd
    assert "ListenStream=" not in slotd
    assert (
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH "
        "CAP_FOWNER CAP_SETGID CAP_SETUID"
    ) in slotd
    assert "AmbientCapabilities=" in slotd
    assert "NoNewPrivileges=yes" not in slotd
    assert "PrivateUsers" not in slotd
    assert "ProtectKernelTunables" not in slotd
    required_rw = (
        "/run/game-control",
        "/run/game-slot",
        "/var/lib/game-control",
        "/var/backups/game-servers",
        "/opt/game-servers",
        "/srv/game-servers",
    )
    for path in required_rw:
        assert path in slotd
    for directive in (
        "User=gamecontrol",
        "Group=gamecontrol",
        "--host ${HorizonWebHost} --port 8444",
        "LoadCredential=proxy-token:/etc/game-control/secrets.d/proxy-token",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "ReadOnlyPaths=/opt/game-control/web",
        "ReadOnlyPaths=/run/game-control",
        "ReadWritePaths=/var/lib/game-control-web",
        "OnFailure=horizon-alert-notify@web.service",
        "LogNamespace=horizon",
        "Slice=horizon.slice",
        "CPUAccounting=yes",
        "MemoryAccounting=yes",
        "IOAccounting=yes",
    ):
        assert directive in web
    forbidden_web_rw = ("/run/game-control", "/run/game-slot", "/var/lib/game-control ", "/var/backups/game-servers")
    rw_lines = [line for line in web.splitlines() if line.startswith("ReadWritePaths=")]
    assert all(not any(path in line for path in forbidden_web_rw) for line in rw_lines)
    assert "WantedBy=" not in web
    assert "crafty" not in slotd.lower()
    assert "pzuser" not in slotd
    for name in ("minecraft-sunlit-cobblemon.service", "terraria-vanilla.service", "terraria-tmod.service"):
        text = _unit(name)
        profile_id = name.removesuffix(".service")
        assert "Environment=GAME_SLOT_REQUIRE_RESERVATION=1" in text
        assert "ExecStart=/usr/local/libexec/game-slot-run " in text
        if profile_id != "minecraft-sunlit-cobblemon":
            assert f"ExecStop=/usr/local/libexec/game-console-stop {profile_id} $MAINPID" in text
        assert "WantedBy=" not in text
        assert "Restart=no" in text
        assert "Slice=games.slice" in text
        assert "MemorySwapMax=0" in text
        assert "CPUAccounting=yes" in text
        assert "MemoryAccounting=yes" in text
        assert "IOAccounting=yes" in text
        assert "LogNamespace=horizon" in text
        assert f"OnFailure=horizon-alert-notify@{profile_id}.service" in text
    sunlit = _unit("minecraft-sunlit-cobblemon.service")
    assert "User=svc-sunlit" in sunlit
    assert "Group=svc-sunlit" in sunlit
    assert "WorkingDirectory=/srv/game-servers/minecraft-sunlit-cobblemon" in sunlit
    assert "ConditionPathExists=/usr/lib/jvm/java-17-openjdk-amd64/bin/java" in sunlit
    assert "Environment=JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64" in sunlit
    assert (
        "ExecStop=-/usr/local/libexec/game-sunlit-stop "
        "minecraft-sunlit-cobblemon $MAINPID"
    ) in sunlit
    assert (
        "ExecStopPost=/usr/local/libexec/game-sunlit-stop "
        "minecraft-sunlit-cobblemon verify"
    ) in sunlit
    assert "MemoryHigh=8G" in sunlit
    assert "MemoryMax=9G" in sunlit
    assert "CPUAccounting=yes" in sunlit
    assert "MemoryAccounting=yes" in sunlit
    assert "IOAccounting=yes" in sunlit
    assert "OOMPolicy=stop" in sunlit
    assert "bore" not in sunlit.lower()
    slice_unit = (UNIT_DIR / "games.slice").read_text()
    assert "CPUAccounting=yes" in slice_unit
    assert "CPUWeight=200" in slice_unit
    assert "MemoryAccounting=yes" in slice_unit
    assert "IOAccounting=yes" in slice_unit
    assert "MemoryHigh=9G" in slice_unit
    assert "MemoryMax=10G" in slice_unit
    assert "MemorySwapMax=0" in slice_unit


def test_sunlit_gc_observability_dropin_is_fixed_and_reversible() -> None:
    dropin = UNIT_DIR / "minecraft-sunlit-cobblemon.service.d" / "gc-telemetry.conf"
    text = dropin.read_text()
    assert "JAVA_TOOL_OPTIONS=-Xlog:gc*:file=/srv/game-servers/minecraft-sunlit-cobblemon-current/logs/gc.log" in text
    assert "filecount=5,filesize=20M" in text
    assert "user_jvm_args.txt" not in text
    assert "-XX:+Use" not in text
    assert "remove this drop-in" in text


def test_phase4_slices_and_fixed_maintenance_template_are_bounded() -> None:
    horizon = _unit("horizon.slice")
    maintenance = _unit("maintenance.slice")
    assert horizon.split("[Slice]", 1)[0].strip() == "[Unit]\nDescription=Horizon control-plane resource boundary"
    assert maintenance.split("[Slice]", 1)[0].strip() == "[Unit]\nDescription=Bounded Horizon maintenance child workload boundary"
    verified = subprocess.run(
        ["/usr/bin/systemd-analyze", "verify", str(UNIT_DIR / "horizon.slice")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unknown key 'Description' in section [Slice]" not in (verified.stdout + verified.stderr)
    verified_maintenance = subprocess.run(
        ["/usr/bin/systemd-analyze", "verify", str(UNIT_DIR / "maintenance.slice")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unknown key 'Description' in section [Slice]" not in (verified_maintenance.stdout + verified_maintenance.stderr)
    assert "MemoryHigh=1G" in horizon and "MemoryMax=2G" in horizon
    assert "MemoryHigh=2G" in maintenance and "MemoryMax=3G" in maintenance
    assert "MemorySwapMax=0" in horizon and "MemorySwapMax=0" in maintenance
    assert "io.max" not in (horizon + maintenance)
    from game_control.interim_maintenance_control import maintenance_argv
    wrapped = maintenance_argv(["/usr/bin/tar", "--create"], slice_name="maintenance.slice", schedulers=("none",))
    assert wrapped[:6] == ["/usr/bin/systemd-run", "--wait", "--pipe", "--quiet", "--service-type=exec", "--slice=maintenance.slice"]
    assert "--" in wrapped and wrapped[-2:] == ["/usr/bin/tar", "--create"]


def test_checkpoint_tmpfiles_and_reversible_topology_manifest() -> None:
    tmpfiles = (ROOT / "ops/tmpfiles/game-control.conf").read_text()
    assert "d /var/lib/game-control/log-checkpoints 0700 root root -" in tmpfiles
    from ops.install import Installer
    expected = Installer(Path("/tmp")).expected_files()
    assert Path("/tmp/etc/systemd/system/horizon.slice") in expected
    assert Path("/tmp/etc/systemd/system/maintenance.slice") in expected


def test_tmodloader_145_candidate_profile_is_private_and_isolated() -> None:
    profile = tomllib.loads(
        (TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.toml").read_text()
    )
    candidate_root = "/srv/game-servers/terraria-tmod-145-ddffee7"
    stable_roots = {"/srv/game-servers/terraria-vanilla", "/srv/game-servers/terraria-tmod"}

    assert profile["id"] == "terraria-tmod-145-candidate"
    assert "public_endpoint" not in profile
    assert profile["ports"] == [{"protocol": "tcp", "port": 7777, "required": True}]
    paths = profile["paths"]
    assert paths["mutable_root"] == candidate_root
    assert paths["log_files"] == [f"{candidate_root}/logs/server.log"]
    assert paths["backup_root"] == "/var/backups/game-servers/terraria-tmod-145-ddffee7"
    assert all(
        not _is_under(path, stable)
        for path in _path_strings(paths)
        for stable in stable_roots
    )
    assert _path_strings(paths["mutable_root"]) == (candidate_root,)
    assert all(_is_under(path, candidate_root) for path in _path_strings(paths["log_files"]))
    assert not _is_under(paths["backup_root"], candidate_root)
    assert paths["data_roots"] == [
        "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7",
        candidate_root,
    ]
    assert profile["process"]["executable"] == (
        "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7/dotnet/dotnet"
    )


def test_tmodloader_145_candidate_runner_is_explicitly_cloned_and_mod_free() -> None:
    runner = json.loads(
        (TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.json").read_text()
    )
    candidate_root = "/srv/game-servers/terraria-tmod-145-ddffee7"
    assert runner["user"] == "tmodloader"
    assert runner["group"] == "tmodloader"
    runtime_root = "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7"
    assert runner["cwd"] == runtime_root
    assert runner["console_fifo"] == "/run/game-slot/terraria-tmod-145-candidate.console"
    assert runner["environment"] == {
        "HOME": f"{candidate_root}/home",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_ROLL_FORWARD": "Disable",
    }
    assert runner["argv"] == [
        f"{runtime_root}/dotnet/dotnet",
        f"{runtime_root}/tModLoader.dll",
        "-server",
        "-config",
        f"{candidate_root}/config/serverconfig.txt",
        "-tmlsavedirectory",
        candidate_root,
        "-world",
        f"{candidate_root}/worlds/swag_central.wld",
        "-modpath",
        f"{candidate_root}/Mods",
    ]


def test_tmodloader_145_candidate_invokes_pinned_dotnet_without_bootstrap() -> None:
    runner = json.loads(
        (TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.json").read_text()
    )
    candidate_root = "/srv/game-servers/terraria-tmod-145-ddffee7"
    runtime_root = "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7"
    assert runner["cwd"] == runtime_root
    assert runner["argv"][:2] == [f"{runtime_root}/dotnet/dotnet", f"{runtime_root}/tModLoader.dll"]
    assert runner["argv"][2] == "-server"
    assert not any("start-tModLoaderServer.sh" in value for value in runner["argv"])
    assert runner["environment"] == {
        "HOME": f"{candidate_root}/home",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_ROLL_FORWARD": "Disable",
    }


def test_tmodloader_145_candidate_unit_is_slot_managed_and_sandboxed() -> None:
    unit = (TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.service").read_text()
    candidate_root = "/srv/game-servers/terraria-tmod-145-ddffee7"
    runtime_root = "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7"
    log_source = f"{candidate_root}/logs/tModLoader-Logs"
    log_target = f"{runtime_root}/tModLoader-Logs"
    assert "ExecStart=/usr/local/libexec/game-slot-run terraria-tmod-145-candidate" in unit
    assert (
        "ExecStop=/usr/local/libexec/game-console-stop terraria-tmod-145-candidate $MAINPID"
        in unit
    )
    for directive in (
        "User=tmodloader",
        "Group=tmodloader",
        f"Environment=HOME={candidate_root}/home",
        "TimeoutStartSec=120",
        "TimeoutStopSec=90",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        f"AssertPathIsDirectory={log_source}",
        f"AssertPathIsDirectory={log_target}",
        f"BindPaths={log_source}:{log_target}",
        f"ReadOnlyPaths=/opt/game-control /etc/game-control /usr/local/libexec /usr/bin /srv/game-servers/terraria-vanilla /srv/game-servers/terraria-tmod",
    ):
        assert directive in unit
    rw_lines = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert len(rw_lines) == 1
    writable_paths = set(rw_lines[0].removeprefix("ReadWritePaths=").split())
    assert writable_paths == {
        "/run/game-control",
        "/run/game-slot",
        candidate_root,
    }
    assert runtime_root not in writable_paths
    assert all(not _is_under(path, runtime_root) for path in writable_paths)
    stable_roots = {"/srv/game-servers/terraria-vanilla", "/srv/game-servers/terraria-tmod"}
    assert all(not _is_under(path, stable) for path in writable_paths for stable in stable_roots)
    assert "terraria-relay" not in unit


def test_tmodloader_145_candidate_contract_is_consistent_across_files() -> None:
    profile_path = TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.toml"
    runner_path = TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.json"
    unit_path = TMODLOADER_145_MIGRATION_DIR / "terraria-tmod-145-candidate.service"
    profile = tomllib.loads(profile_path.read_text())
    runner = json.loads(runner_path.read_text())
    unit = unit_path.read_text()
    profile_id = profile["id"]
    candidate_root = profile["paths"]["mutable_root"]

    assert profile_path.stem == runner_path.stem == profile_id
    assert profile["systemd_unit"] == unit_path.name == f"{profile_id}.service"
    assert _directive(unit, "Description") == f"{profile['display_name']} game server"
    assert _directive(unit, "ExecStart") == f"/usr/local/libexec/game-slot-run {profile_id}"
    assert _directive(unit, "ExecStop") == (
        f"/usr/local/libexec/game-console-stop {profile_id} $MAINPID"
    )

    assert runner["user"] == runner["group"] == "tmodloader"
    assert _directive(unit, "User") == runner["user"]
    assert _directive(unit, "Group") == runner["group"]
    assert profile["start_timeout_seconds"] == int(_directive(unit, "TimeoutStartSec"))
    assert profile["stop_timeout_seconds"] == int(_directive(unit, "TimeoutStopSec"))

    runtime_root = "/opt/game-servers/terraria-tmod/versions/1.4.5-ddffee7"
    assert runner["cwd"] == runtime_root
    assert _directive(unit, "WorkingDirectory") == candidate_root
    assert _directive(unit, "Environment") == f"HOME={candidate_root}/home"
    assert runner["console_fifo"] == f"/run/game-slot/{profile_id}.console"
    expected_log = profile["paths"]["log_files"][0]
    assert _directive(unit, "StandardOutput") == f"append:{expected_log}"
    assert _directive(unit, "StandardError") == f"append:{expected_log}"
    assert candidate_root in profile["paths"]["data_roots"]
    assert not _is_under(profile["paths"]["backup_root"], candidate_root)

    process = profile["process"]
    assert runner["argv"] == [process["executable"], *process["argv_contains"]]
    assert runner["argv"][0].endswith("/dotnet/dotnet")
    assert runner["argv"][1].endswith("/tModLoader.dll")
    assert runner["argv"][2:4] == ["-server", "-config"]
    assert runner["argv"][5] == "-tmlsavedirectory"
    assert runner["argv"][7] == "-world"
    assert runner["argv"][9] == "-modpath"
    assert runner["argv"][4] == f"{candidate_root}/config/serverconfig.txt"
    assert runner["argv"][6] == candidate_root
    assert runner["argv"][8] == f"{candidate_root}/worlds/swag_central.wld"
    assert runner["argv"][10] == f"{candidate_root}/Mods"
    assert runner["environment"] == {
        "HOME": f"{candidate_root}/home",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_ROLL_FORWARD": "Disable",
    }

    assert (TMODLOADER_145_MIGRATION_DIR / "expected-warnings.txt").read_text() == ""


def test_tmodloader_145_candidate_warning_allowlist_starts_empty() -> None:
    assert (TMODLOADER_145_MIGRATION_DIR / "expected-warnings.txt").read_text() == ""


def test_installer_manifest_is_explicitly_three_profile_and_legacy_free() -> None:
    from ops.install import ACTIVE_PROFILE_IDS, Installer, LEGACY_TARGETS, WEB_FILES

    installer = Installer(Path("/tmp"))
    expected = installer.expected_files()
    profile_targets = sorted(
        path.name for path in expected if "/profiles.d/" in str(path)
    )
    runner_targets = sorted(
        path.name for path in expected if "/runner.d/" in str(path)
    )
    unit_targets = sorted(
        path.name
        for path in expected
        if str(path).startswith("/tmp/etc/systemd/") and path.suffix in {".service", ".slice", ".timer"}
    )
    assert ACTIVE_PROFILE_IDS == (
        "minecraft-sunlit-cobblemon",
        "terraria-vanilla",
        "terraria-tmod",
    )
    assert profile_targets == sorted(f"{profile_id}.toml" for profile_id in ACTIVE_PROFILE_IDS)
    assert runner_targets == sorted(f"{profile_id}.json" for profile_id in ACTIVE_PROFILE_IDS)
    assert Path("/tmp/etc/systemd/system/minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf") in expected
    assert unit_targets == [
        "bore-minecraft-fenced.service",
        "game-control-web.service",
        "game-slotd.service",
        "games.slice",
        "horizon-alert-drill@.service",
        "horizon-alert-notify@.service",
        "horizon-bore-liveness.service",
        "horizon-bore-liveness.timer",
        "horizon-sunlit-auto-update.service",
        "horizon-sunlit-auto-update.timer",
        "horizon-terraria-relay.service",
        "horizon.slice",
        "lazymc-minecraft.service",
        "maintenance.slice",
        "minecraft-sunlit-cobblemon.service",
        "terraria-tmod.service",
        "terraria-vanilla.service",
    ]
    assert expected[Path("/tmp/etc/nftables.conf")][0] == NFTABLES
    assert Path("/tmp/usr/local/libexec/game-sunlit-stop") in expected
    assert Path("/tmp/usr/local/libexec/game-sunlit-prepare") in expected
    assert Path("/tmp/usr/local/libexec/horizon-capability-issue") in expected
    assert Path("/tmp/etc/game-control/lazymc/server.properties") in expected
    assert Path("/tmp/usr/local/libexec/horizon-alert-notify") in expected
    assert Path("/tmp/usr/local/libexec/horizon-bore-liveness") in expected
    assert Path("/tmp/usr/local/libexec/horizon-state-migrate") in expected
    assert {
        path.name: expected[Path(f"/tmp/opt/game-control/web/{path.name}")]
        for path in WEB_FILES
    } == {path.name: (path, 0o644) for path in WEB_FILES}
    assert all(not any(legacy in str(path) for legacy in LEGACY_TARGETS) for path in expected)
    assert all("crafty-token" not in str(path) and "pz-rising" not in str(path) for path in expected)


def test_vm_config_has_exact_staggered_daily_protected_b2_schedules() -> None:
    config = tomllib.loads((ROOT / "config/game-control.toml").read_text())
    schedules = config["schedule"]
    assert config["boot_autostart"] is False
    assert {entry["profile"] for entry in schedules} == {
        "minecraft-sunlit-cobblemon",
        "terraria-vanilla",
        "terraria-tmod",
    }
    assert len(schedules) == 3
    assert all(entry.get("enabled", True) is True for entry in schedules)
    assert all(entry["backup_destination"] == "horizon-b2" for entry in schedules)
    assert {tuple(entry["cron"].split()[:2]) for entry in schedules} == {
        ("10", "3"),
        ("20", "3"),
        ("40", "3"),
    }
    assert all(entry["cron"].split()[2:] == ["*", "*", "*"] for entry in schedules)
    assert "crafty" not in config
    assert all("pz" not in repr(entry).lower() for entry in schedules)
    assert config["rcon"] == {
        "host": "127.0.0.1",
        "port": 25575,
        "password_path": "/etc/game-control/secrets.d/minecraft-rcon-password",
    }


def test_vm_config_has_fixed_sunlit_benchmark_plan() -> None:
    config = tomllib.loads((ROOT / "config/game-control.toml").read_text())
    plans = config["benchmark"]
    assert plans == [
        {
            "profile": "minecraft-sunlit-cobblemon",
            "driver": "/usr/local/libexec/swagbench-ab",
            "config": "/etc/game-control/swagbench.json",
            "report_root": "/srv/game-servers/minecraft-sunlit-benchmark/.horizon-reports",
            "timeout_seconds": 21600,
            "presets": [
                {"id": "current", "label": "Current production"},
                {"id": "balanced-g1", "label": "Balanced G1 candidate"},
            ],
        }
    ]


def test_installer_provisions_writable_runtime_state_directories() -> None:
    from ops.install import Installer

    directories = {
        path: (mode, user, group)
        for path, mode, user, group in Installer(Path("/tmp")).directories()
    }
    assert directories[Path("/tmp/opt/game-control/web")] == (0o755, "root", "root")
    paths = set(directories)
    assert Path("/tmp/srv/game-servers/terraria-vanilla/.local/share/Terraria") in paths
    assert Path("/tmp/srv/game-servers/terraria-tmod/.local/share/Terraria") in paths
    assert Path("/tmp/opt/game-servers/minecraft-sunlit-cobblemon") in paths
    assert Path("/tmp/opt/game-servers/minecraft-sunlit-cobblemon/releases") in paths
    assert Path("/tmp/srv/game-servers/minecraft-sunlit-cobblemon") in paths
    assert Path("/tmp/var/backups/game-servers/minecraft-sunlit-cobblemon") in paths
    assert directories[Path("/tmp/opt/game-servers/minecraft-sunlit-cobblemon")] == (0o755, "root", "root")
    assert directories[Path("/tmp/opt/game-servers/minecraft-sunlit-cobblemon/releases")] == (0o755, "root", "root")
    assert directories[Path("/tmp/srv/game-servers/minecraft-sunlit-cobblemon")] == (0o750, "svc-sunlit", "svc-sunlit")
    assert directories[Path("/tmp/srv/game-servers/terraria-vanilla")] == (
        0o750,
        "terraria-vanilla",
        "terraria-vanilla",
    )
    assert directories[Path("/tmp/srv/game-servers/terraria-tmod")] == (
        0o750,
        "tmodloader",
        "tmodloader",
    )
    assert directories[Path("/tmp/var/backups/game-servers/minecraft-sunlit-cobblemon")] == (0o700, "root", "root")
    assert all(user not in {"crafty", "pzuser"} for _mode, user, _group in directories.values())


def test_sunlit_forge_relative_libraries_are_bridged_from_the_writable_cwd() -> None:
    from ops.install import Installer, SUNLIT_LIBRARIES_LINK, SUNLIT_LIBRARIES_TARGET

    installer = Installer(Path("/tmp"))
    assert installer.expected_links() == {
        Path("/tmp" + SUNLIT_LIBRARIES_LINK): SUNLIT_LIBRARIES_TARGET,
    }
    runner = json.loads((RUNNER_DIR / "minecraft-sunlit-cobblemon.json").read_text())
    assert runner["cwd"] == "/srv/game-servers/minecraft-sunlit-cobblemon-current"
    assert runner["argv"][3].startswith("@/opt/game-servers/minecraft-sunlit-cobblemon/")
    assert "libraries/" in runner["argv"][3]
    assert runner["environment"]["HOME"] == "/srv/game-servers/minecraft-sunlit-cobblemon-state/local"
    assert "L /srv/game-servers/minecraft-sunlit-cobblemon/libraries - - - - /opt/game-servers/minecraft-sunlit-cobblemon/libraries" in (
        ROOT / "ops" / "tmpfiles" / "game-control.conf"
    ).read_text()


def test_tmpfiles_preserves_root_owned_lock_parent_boundary() -> None:
    content = (ROOT / "ops" / "tmpfiles" / "game-control.conf").read_text()
    expected = {
        "d /var/lib/game-control 0700 root root -",
        "d /var/lib/game-control-web 0700 gamecontrol gamecontrol -",
        "d /run/game-control 0755 root root -",
        "d /run/game-slot 0770 root gameslot -",
        "f /run/game-control/operation.lock 0660 root gameslot -",
        "f /run/game-control/slot.lock 0660 root gameslot -",
        "f /run/game-control/reservation.json 0644 root root -",
        "L /srv/game-servers/minecraft-sunlit-cobblemon/libraries - - - - /opt/game-servers/minecraft-sunlit-cobblemon/libraries",
    }
    assert expected <= set(content.splitlines())
    assert "d /run 0770" not in content


def test_installer_check_is_read_only_and_apply_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    installer = ROOT / "ops" / "install.py"
    env = {**os.environ, "GAME_CONTROL_INSTALL_ROOT": str(root)}
    check = subprocess.run([sys.executable, str(installer), "--check"], env=env, text=True, capture_output=True)
    assert check.returncode != 0
    assert list(root.rglob("*")) == []
    apply = subprocess.run(
        [sys.executable, str(installer), "--apply", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert apply.returncode == 0, apply.stderr
    second = subprocess.run(
        [sys.executable, str(installer), "--check", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert second.returncode != 0
    assert "fixed B2 secret" in second.stdout
    assert str(root / "etc/game-control/secrets.d/horizon-b2-rclone.conf") not in second.stdout
    secret = root / "etc/game-control/secrets.d/horizon-b2-rclone.conf"
    assert not secret.exists()
    secret.write_bytes(b"out-of-band fixture\n")
    os.chmod(secret, 0o600)
    ready = subprocess.run(
        [sys.executable, str(installer), "--check", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert ready.returncode == 0, ready.stderr
    apply_again = subprocess.run(
        [sys.executable, str(installer), "--apply", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert apply_again.returncode == 0, apply_again.stderr
    assert secret.read_bytes() == b"out-of-band fixture\n"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert secret.stat().st_nlink == 1
    libraries = root / "srv/game-servers/minecraft-sunlit-cobblemon/libraries"
    assert libraries.is_symlink()
    assert os.readlink(libraries) == "/opt/game-servers/minecraft-sunlit-cobblemon/libraries"
    tmod_log_anchor = (
        root
        / "srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor"
    )
    assert tmod_log_anchor.is_file()
    assert stat.S_IMODE(tmod_log_anchor.stat().st_mode) == 0o600
    assert not (root / "etc/game-control/secrets.d/crafty-token").exists()
    assert not (root / "etc/game-control/profiles.d/minecraft.toml").exists()
    assert not (root / "etc/game-control/profiles.d/pz-rising.toml").exists()


def test_installer_applies_runtime_sources_verifier_and_safe_stale_cleanup(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = ROOT / "ops" / "install.py"
    env = {**os.environ, "GAME_CONTROL_INSTALL_ROOT": str(root)}
    apply = subprocess.run(
        [sys.executable, str(installer), "--apply", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert apply.returncode == 0, apply.stderr

    for relative in (
        "src/game_control/interim_maintenance_control.py",
        "src/game_control/introspection.py",
        "src/game_control/telemetry_db.py",
        "scripts/verify-deployed.py",
    ):
        source = ROOT / relative
        destination = root / "opt/game-control" / relative
        assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE((root / "opt/game-control/scripts/verify-deployed.py").stat().st_mode) == 0o600
    manifest = root / "opt/game-control/.horizon-runtime-manifest"
    assert manifest.is_file()
    assert "src/game_control/introspection.py" in manifest.read_text()

    unmanaged = root / "opt/game-control/src/game_control/user_extension.py"
    unmanaged.write_text("user-owned\n")
    stale = root / "opt/game-control/src/game_control/removed.py"
    stale.write_text("old-managed\n")
    digest = hashlib.sha256(stale.read_bytes()).hexdigest()
    with manifest.open("a", encoding="ascii") as stream:
        stream.write("1" + chr(9) + f"src/game_control/removed.py{chr(9)}{digest}{chr(9)}0644" + chr(10))

    second = subprocess.run(
        [sys.executable, str(installer), "--apply", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert second.returncode == 0, second.stderr
    assert not stale.exists()
    assert unmanaged.read_text() == "user-owned\n"

    secret = root / "etc/game-control/secrets.d/horizon-b2-rclone.conf"
    secret.write_bytes(b"opaque fixture\n")
    os.chmod(secret, 0o600)
    check = subprocess.run(
        [sys.executable, str(installer), "--check", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert check.returncode == 0, check.stderr

    runtime_file = root / "opt/game-control/src/game_control/introspection.py"
    runtime_file.write_text("tampered runtime source\n")
    drift = subprocess.run(
        [sys.executable, str(installer), "--check", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert drift.returncode != 0
    assert f"content drift {runtime_file}" in drift.stdout


def test_installer_replaces_managed_systemd_units_during_reconciliation(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    destination = root / "etc/systemd/system/game-slotd.service"
    destination.write_text(destination.read_text().replace("Slice=horizon.slice", "Slice=system.slice"), encoding="utf-8")
    installer.apply()
    assert destination.read_bytes() == (ROOT / "ops/systemd/game-slotd.service").read_bytes()
    maintenance = root / "etc/systemd/system/maintenance.slice"
    maintenance.write_text(maintenance.read_text().replace("[Unit]", "[Slice]", 1), encoding="utf-8")
    installer.apply()
    assert maintenance.read_bytes() == (ROOT / "ops/systemd/maintenance.slice").read_bytes()


def test_installed_installer_is_self_contained_for_check_and_idempotent_reapply(tmp_path: Path) -> None:
    installer = ROOT / "ops" / "install.py"
    root = tmp_path / "root"
    env = {**os.environ, "GAME_CONTROL_INSTALL_ROOT": str(root)}
    first = subprocess.run(
        [sys.executable, str(installer), "--apply", "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert first.returncode == 0, first.stderr

    installed = root / "opt/game-control/ops/install.py"
    assert installed.is_file()
    mirror_paths = (
        "config/profiles/terraria-vanilla.toml",
        "config/runner/terraria-tmod.json",
        "ops/systemd/game-slotd.service",
        "ops/systemd/game-slotd.service.d/io-metrics.conf",
        "ops/bin/game-slot-run",
        "ops/lazymc/lazymc.toml",
        "ops/tmpfiles/game-control.conf",
        "ops/nftables/horizon.nft",
        "ops/journald/horizon.conf",
        "web/app.js",
        "pyproject.toml",
        "src/game_control/adapters/crafty.py",
        "scripts/verify-deployed.py",
    )
    for relative in mirror_paths:
        assert (root / "opt/game-control" / relative).is_file(), relative

    secret = root / "etc/game-control/secrets.d/horizon-b2-rclone.conf"
    secret.write_bytes(b"offline fixture\n")
    os.chmod(secret, 0o600)

    check = subprocess.run(
        [sys.executable, str(installed), "--check", "--root", str(root), "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert check.returncode == 0, check.stderr + check.stdout
    second = subprocess.run(
        [sys.executable, str(installed), "--apply", "--root", str(root), "--skip-systemd-verify"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert second.returncode == 0, second.stderr + second.stdout


def test_phase2_readonly_harnesses_are_packaged_and_executable(tmp_path: Path) -> None:
    from ops.install import Installer
    installer = Installer(tmp_path / "root", skip_systemd_verify=True)
    installer.apply()
    for name in ("horizon-phase2-threshold", "horizon-phase2-collect",
                 "horizon-phase2-browser-evidence", "horizon-phase2-live-acceptance"):
        destination = tmp_path / "root/usr/local/libexec" / name
        assert destination.is_file(), name
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_libexec_directory_is_searchable_by_game_service_users(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    libexec = root / "usr/local/libexec"
    assert libexec.is_dir()
    assert stat.S_IMODE(libexec.stat().st_mode) == 0o755


def test_phase1_phase4_operator_helpers_are_packaged_with_expected_modes(tmp_path: Path) -> None:
    from ops.install import Installer
    installer = Installer(tmp_path / "root", skip_systemd_verify=True)
    installer.apply()
    for name in ("horizon-jvm-args", "horizon-telemetry-migrate"):
        destination = tmp_path / "root/usr/local/libexec" / name
        assert destination.is_file(), name
        assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_runtime_manifest_fails_closed_for_changed_stale_and_unsafe_paths(tmp_path: Path) -> None:
    from ops.install import Installer

    installer = Installer(tmp_path / "root")
    with pytest.raises(RuntimeError, match="invalid runtime manifest path"):
        installer._runtime_destination("../outside")

    runtime_root = installer.target("/opt/game-control")
    runtime_root.mkdir(parents=True)
    (runtime_root / "src").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(RuntimeError, match="runtime path contains symlink"):
        installer._ensure_runtime_parent(runtime_root / "src/game_control")

    escaped_root = tmp_path / "escaped-root"
    escaped_root.mkdir()
    (escaped_root / "opt").symlink_to(tmp_path / "outside-opt", target_is_directory=True)
    with pytest.raises(RuntimeError, match="runtime path contains symlink"):
        Installer(escaped_root)._ensure_runtime_parent(escaped_root / "opt/game-control/src")

    safe_root = tmp_path / "safe-root"
    apply = Installer(safe_root, skip_systemd_verify=True)
    apply.apply()
    manifest = apply._runtime_manifest()
    stale = safe_root / "opt/game-control/src/game_control/changed.py"
    stale.write_text("user-edited\n")
    with manifest.open("a", encoding="ascii") as stream:
        stream.write("1" + chr(9) + "src/game_control/changed.py" + chr(9) + "0" * 64 + chr(9) + "0644" + chr(10))
    with pytest.raises(RuntimeError, match="requires manual review|changed"):
        apply.apply()
    assert stale.read_text() == "user-edited\n"


def test_runtime_manifest_check_validates_records_ownership_and_links(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    manifest = installer._runtime_manifest()
    records = manifest.read_text(encoding="ascii").splitlines()
    manifest.unlink()
    assert any("runtime manifest is absent" in problem for problem in installer.drift())
    installer.apply()

    manifest.write_text("malformed\n", encoding="ascii")
    assert any("runtime manifest is malformed" in problem for problem in installer.drift())
    with pytest.raises(RuntimeError, match="invalid runtime manifest record"):
        installer.apply()
    manifest.unlink()
    installer.apply()

    manifest.write_text("\n".join(records[:-1]) + "\n", encoding="ascii")
    assert "runtime manifest path set drift" in installer.drift()
    installer.apply()

    first = records[0].split("\t")
    first[2] = "0" * 64
    manifest.write_text("\n".join(["\t".join(first), *records[1:]]) + "\n", encoding="ascii")
    assert any("runtime manifest record drift" in problem for problem in installer.drift())
    installer.apply()

    runtime_file = root / "opt/game-control/src/game_control/introspection.py"
    if os.geteuid() == 0:
        os.chown(runtime_file, 65534, 65534)
        assert any(f"ownership drift {runtime_file}" in problem for problem in installer.drift())
        installer.apply()
        web_directory = root / "opt/game-control/web"
        os.chown(web_directory, 65534, 65534)
        assert any(f"ownership drift {web_directory}" in problem for problem in installer.drift())
        installer.apply()
    hard_link = runtime_file.with_name("introspection.link")
    os.link(runtime_file, hard_link)
    assert any(f"link count drift {runtime_file}" in problem for problem in installer.drift())
    hard_link.unlink()


def test_runtime_stale_dangling_symlink_requires_manual_review(tmp_path: Path) -> None:
    from ops.install import Installer

    installer = Installer(tmp_path / "root", skip_systemd_verify=True)
    installer.apply()
    manifest = installer._runtime_manifest()
    symlink = installer.target("/opt/game-control/src/game_control/removed.py")
    symlink.symlink_to("missing-target")
    with manifest.open("a", encoding="ascii") as stream:
        stream.write("1" + chr(9) + "src/game_control/removed.py" + chr(9) + "0" * 64 + chr(9) + "0644" + chr(10))
    with pytest.raises(RuntimeError, match="requires manual review"):
        installer.apply()
    assert symlink.is_symlink()


def _filesystem_fingerprint(root: Path) -> dict[str, tuple[object, ...]]:
    fingerprint: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fingerprint[relative] = ("symlink", os.readlink(path), stat.S_IMODE(info.st_mode), info.st_nlink)
        elif stat.S_ISDIR(info.st_mode):
            fingerprint[relative] = ("directory", stat.S_IMODE(info.st_mode), info.st_nlink)
        elif stat.S_ISREG(info.st_mode):
            fingerprint[relative] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_nlink)
        else:
            fingerprint[relative] = ("other", info.st_mode, info.st_nlink)
    return fingerprint


def test_installer_rejects_symlinked_root_and_ancestor_before_any_mutation(tmp_path: Path) -> None:
    from ops.install import Installer

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside-preserved\n")

    root = tmp_path / "root-link"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="install root is symlinked"):
        Installer(root, skip_systemd_verify=True).apply()
    assert sentinel.read_bytes() == b"outside-preserved\n"
    assert root.is_symlink()

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(outside, target_is_directory=True)
    selected = parent_link / "alternate-root"
    before = _filesystem_fingerprint(tmp_path)
    with pytest.raises(RuntimeError, match="install root is symlinked"):
        Installer(selected, skip_systemd_verify=True).apply()
    assert _filesystem_fingerprint(tmp_path) == before
    assert sentinel.read_bytes() == b"outside-preserved\n"


@pytest.mark.parametrize(
    "relative",
    (
        "opt",
        "opt/game-control",
        "etc",
        "etc/game-control",
        "etc/systemd/system",
        "usr/local/libexec",
        "var/lib/game-control",
        "srv/game-servers",
    ),
)
def test_installer_rejects_non_directory_managed_ancestor_before_any_mutation(
    tmp_path: Path, relative: str
) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    root.mkdir()
    ancestor = root / relative
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    ancestor.write_bytes(b"not-a-directory\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"outside-preserved\n")
    before = _filesystem_fingerprint(tmp_path)

    with pytest.raises(RuntimeError, match="managed parent is not a directory"):
        Installer(root, skip_systemd_verify=True).apply()

    assert _filesystem_fingerprint(tmp_path) == before


@pytest.mark.parametrize(
    "relative",
    (
        "etc/game-control/profiles.d",
        "etc/systemd/system",
        "opt/game-control",
        "opt/game-control/web",
        "etc/game-control/secrets.d",
        "usr/local/libexec",
        "usr/lib/tmpfiles.d",
        "usr/local/share/horizon",
        "srv/game-servers/minecraft-sunlit-cobblemon",
    ),
)
def test_installer_rejects_symlinked_managed_parent_before_any_mutation(
    tmp_path: Path, relative: str
) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"outside-preserved\n")
    parent = root / relative
    parent.parent.mkdir(parents=True)
    parent.symlink_to(outside, target_is_directory=True)
    before = _filesystem_fingerprint(tmp_path)

    with pytest.raises(RuntimeError, match="managed parent is symlinked"):
        Installer(root, skip_systemd_verify=True).apply()

    assert _filesystem_fingerprint(tmp_path) == before
    assert (outside / "sentinel").read_bytes() == b"outside-preserved\n"


@pytest.mark.parametrize(
    "relative",
    (
        "etc/systemd/system/game-slotd.service",
        "opt/game-control/src/game_control/introspection.py",
        "srv/game-servers/terraria-vanilla/logs/server.log",
        "srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor",
        "opt/game-control/.horizon-runtime-manifest",
    ),
)
@pytest.mark.parametrize("kind", ("symlink", "hardlink", "directory"))
def test_installer_rejects_unsafe_managed_destination_before_replacement(
    tmp_path: Path, relative: str, kind: str
) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside-preserved\n")
    destination = root / relative
    destination.unlink()
    if kind == "symlink":
        destination.symlink_to(outside_sentinel)
    elif kind == "hardlink":
        os.link(outside_sentinel, destination)
    else:
        destination.mkdir()
    managed = root / "etc/systemd/system/game-control-web.service"
    managed.write_bytes(b"managed-before-preflight\n")
    before = _filesystem_fingerprint(root)
    outside_before = _filesystem_fingerprint(outside)

    with pytest.raises(RuntimeError, match="managed destination|managed anchor|runtime manifest"):
        installer.apply()

    assert _filesystem_fingerprint(root) == before
    assert _filesystem_fingerprint(outside) == outside_before


@pytest.mark.parametrize(
    ("relative", "kind"),
    (
        ("etc/game-control/secrets.d/minecraft-rcon-password", "symlink"),
        ("etc/game-control/secrets.d/minecraft-rcon-password", "hardlink"),
        ("etc/game-control/secrets.d/minecraft-rcon-password", "directory"),
        ("etc/game-control/secrets.d/horizon-b2-rclone.conf", "symlink"),
        ("srv/game-servers/minecraft-sunlit-cobblemon/libraries", "wrong-link"),
        ("srv/game-servers/minecraft-sunlit-cobblemon/libraries", "hardlink"),
    ),
)
def test_installer_rejects_unsafe_secret_or_link_destination_before_mutation(
    tmp_path: Path, relative: str, kind: str
) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "sentinel"
    outside_sentinel.write_bytes(b"outside-preserved\n")
    destination = root / relative
    if destination.is_dir() and not destination.is_symlink():
        destination.rmdir()
    else:
        destination.unlink(missing_ok=True)
    if kind == "symlink":
        destination.symlink_to(outside_sentinel)
    elif kind == "wrong-link":
        destination.symlink_to("wrong-target")
    elif kind == "hardlink":
        os.link(outside_sentinel, destination)
    else:
        destination.mkdir()
    before = _filesystem_fingerprint(root)
    outside_before = _filesystem_fingerprint(outside)

    with pytest.raises(RuntimeError, match="secret|managed link|symlink"):
        installer.apply()

    assert _filesystem_fingerprint(root) == before
    assert _filesystem_fingerprint(outside) == outside_before


def test_installer_rejects_stale_runtime_hardlink_before_removal_or_copy(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_alias = outside / "stale-alias"
    stale = root / "opt/game-control/src/game_control/removed.py"
    stale.write_bytes(b"unchanged-stale\n")
    os.link(stale, outside_alias)
    digest = hashlib.sha256(stale.read_bytes()).hexdigest()
    with installer._runtime_manifest().open("a", encoding="ascii") as stream:
        stream.write("1" + chr(9) + "src/game_control/removed.py" + chr(9) + digest + chr(9) + "0644" + chr(10))
    managed = root / "etc/systemd/system/game-control-web.service"
    managed.write_bytes(b"managed-before-preflight\n")
    before = _filesystem_fingerprint(root)
    outside_before = _filesystem_fingerprint(outside)

    with pytest.raises(RuntimeError, match="unexpected link count"):
        installer.apply()

    assert _filesystem_fingerprint(root) == before
    assert _filesystem_fingerprint(outside) == outside_before
    assert stale.exists()
    assert outside_alias.exists()


def test_runtime_manifest_reader_rejects_dangling_symlink_without_mutation(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    manifest = root / "opt/game-control/.horizon-runtime-manifest"
    manifest.parent.mkdir(parents=True)
    manifest.symlink_to(tmp_path / "outside-missing")
    before = _filesystem_fingerprint(tmp_path)

    with pytest.raises(RuntimeError, match="runtime manifest is not a regular file"):
        Installer(root)._read_runtime_manifest()

    assert _filesystem_fingerprint(tmp_path) == before


def test_installer_rejects_declared_file_directory_collision_before_mutation(tmp_path: Path, monkeypatch) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    collision = installer.target("/etc/game-control/game-control.toml")
    original_directories = installer.directories
    monkeypatch.setattr(
        installer,
        "directories",
        lambda: (*original_directories(), (collision, 0o755, "root", "root")),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"outside-preserved\n")
    before = _filesystem_fingerprint(tmp_path)

    with pytest.raises(RuntimeError, match="managed file/directory target collision"):
        installer.apply()

    assert _filesystem_fingerprint(tmp_path) == before


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_preflight_sources_rejects_source_links_without_target_mutation(tmp_path: Path, kind: str) -> None:
    from ops.install import Installer

    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "source"
    source.write_bytes(b"source\n")
    if kind == "symlink":
        linked_source = source_root / "linked-source"
        linked_source.symlink_to(source)
    else:
        linked_source = source_root / "linked-source"
        os.link(source, linked_source)
    destination = tmp_path / "destination"
    destination.write_bytes(b"destination-preserved\n")

    with pytest.raises(RuntimeError, match="package source"):
        Installer._preflight_sources({destination: (linked_source, 0o644)})

    assert destination.read_bytes() == b"destination-preserved\n"
    assert source.read_bytes() == b"source\n"


def test_installer_secret_drift_checks_fail_closed_without_reading_content(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    secret = root / "etc/game-control/secrets.d/horizon-b2-rclone.conf"
    secret.parent.mkdir(parents=True)

    def secret_problems() -> list[str]:
        return [item for item in Installer(root).drift() if item.startswith("fixed B2 secret")]

    assert secret_problems() == ["fixed B2 secret is absent"]
    secret.write_bytes(b"opaque\n")
    os.chmod(secret, 0o644)
    assert secret_problems() == ["fixed B2 secret mode drift"]

    os.chmod(secret, 0o600)
    hard_link = secret.with_name("hard-link")
    os.link(secret, hard_link)
    assert secret_problems() == ["fixed B2 secret has unexpected link count"]
    hard_link.unlink()
    secret.unlink()
    secret.symlink_to("elsewhere")
    assert secret_problems() == ["fixed B2 secret is symlinked"]
    secret.unlink()
    secret.mkdir()
    assert secret_problems() == ["fixed B2 secret is not a regular file"]
    secret.rmdir()
    secret.write_bytes(b"opaque\n")
    os.chmod(secret, 0o600)
    if os.geteuid() == 0:
        os.chown(secret, 65534, 65534)
        assert secret_problems() == ["fixed B2 secret ownership drift"]


def test_installer_static_and_runtime_projections_match_canonical_manifest(tmp_path: Path) -> None:
    from game_control.deployment_manifest import get_manifest
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    manifest = get_manifest()
    assert set(installer._expected_install_files()) == {
        Path(spec.target) for spec in manifest.files_for(root)
    }
    assert set(installer.runtime_files()) == {
        Path(spec.target) for spec in manifest.runtime_files_for(root)
    }
    assert set(installer.expected_links()) == {spec.target_path(root) for spec in manifest.symlinks}
    assert len(installer.directories()) == len(manifest.directories)


def test_installer_drift_reports_alternate_root_directory_ownership(tmp_path: Path) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    os.chown(root / "var/lib/game-control-web", 65534, 65534)
    assert any("ownership drift" in problem for problem in installer.drift())


def test_installer_staged_ownership_is_independent_of_host_accounts(tmp_path: Path, monkeypatch) -> None:
    from ops.install import Installer

    root = tmp_path / "root"
    installer = Installer(root, skip_systemd_verify=True)
    installer.apply()
    monkeypatch.setattr(installer, "_lookup", lambda *_args, **_kwargs: 1234)
    assert not any("ownership drift" in problem for problem in installer.drift())
