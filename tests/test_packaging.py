from __future__ import annotations

import json
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


ROOT = Path(__file__).parents[1]
PROFILE_DIR = ROOT / "config" / "profiles"
RUNNER_DIR = ROOT / "config" / "runner"
UNIT_DIR = ROOT / "ops" / "systemd"


def _profiles() -> dict[str, Profile]:
    return {
        path.stem: Profile.model_validate(tomllib.loads(path.read_text()))
        for path in sorted(PROFILE_DIR.glob("*.toml"))
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
    assert set(profiles) == {"minecraft", "pz-rising", "terraria-vanilla", "terraria-tmod"}
    assert profiles["minecraft"].adapter is AdapterKind.CRAFTY
    assert str(profiles["minecraft"].crafty_server_id) == "00000000-0000-4000-8000-000000000001"
    assert profiles["minecraft"].ports[0].protocol == "tcp"
    assert profiles["minecraft"].ports[0].port == 25565
    assert profiles["minecraft"].public_endpoint is not None
    assert profiles["minecraft"].public_endpoint.host == "mc.example.com"
    assert profiles["minecraft"].public_endpoint.port == 25565
    assert profiles["minecraft"].update.kind == "manual"
    assert OperationName.UPDATE_APPLY not in profiles["minecraft"].operations
    assert profiles["minecraft"].paths.data_roots == (
        Path("/srv/game-servers/minecraft"),
    )
    assert profiles["pz-rising"].systemd_unit == "pz-rising.service"
    assert {(p.protocol, p.port) for p in profiles["pz-rising"].ports} == {
        ("udp", 16261),
        ("udp", 16262),
    }
    assert profiles["pz-rising"].public_endpoint is not None
    assert profiles["pz-rising"].public_endpoint.host == "pz.example.com"
    assert profiles["pz-rising"].public_endpoint.port == 16261
    assert profiles["pz-rising"].update.kind == "steamcmd_in_place"
    assert profiles["pz-rising"].update.app_id == 380870
    assert profiles["pz-rising"].update.beta == "unstable"
    assert set(profiles["pz-rising"].operations) == {
        OperationName.START,
        OperationName.STOP,
        OperationName.RESTART,
        OperationName.FORCE_STOP,
        OperationName.BACKUP,
        OperationName.RESTORE,
        OperationName.UPDATE_CHECK,
        OperationName.UPDATE_APPLY,
        OperationName.COMMAND,
    }
    assert profiles["terraria-vanilla"].systemd_unit == "terraria-vanilla.service"
    assert str(profiles["terraria-vanilla"].update.download_url) == (
        "https://terraria.org/api/download/pc-dedicated-server/terraria-server-1456.zip"
    )
    assert OperationName.CLONE_SOURCE in profiles["terraria-vanilla"].operations
    assert OperationName.CLONE_TARGET not in profiles["terraria-vanilla"].operations
    assert profiles["terraria-tmod"].systemd_unit == "terraria-tmod.service"
    for profile_id in ("terraria-vanilla", "terraria-tmod"):
        profile = profiles[profile_id]
        assert {(p.protocol, p.port) for p in profile.ports} == {("tcp", 7777)}
        assert profile.update.kind == "release_symlink"
        assert profile.public_endpoint is not None
        assert profile.public_endpoint.host == "terraria.example.com"


def test_runner_configs_are_fixed_and_secret_free() -> None:
    expected = {
        "minecraft": {
            "user": "crafty",
            "cwd": "/srv/game-servers/minecraft",
            "argv": ["/usr/bin/bash", "start.sh"],
        },
        "pz-rising": {"user": "pzuser", "cwd": "/opt/pzserver"},
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
            "cwd": "/srv/game-servers/terraria-tmod",
            "argv": [
                "/opt/game-servers/terraria-tmod/current/start-tModLoaderServer.sh",
                "-nosteam",
                "-config",
                "/srv/game-servers/terraria-tmod/config/serverconfig.txt",
                    "-tmlsavedirectory",
                    "/srv/game-servers/terraria-tmod",
            ],
        },
    }
    for profile_id, contract in expected.items():
        config = json.loads((RUNNER_DIR / f"{profile_id}.json").read_text())
        expected_keys = {"user", "group", "cwd", "argv", "environment"}
        if profile_id.startswith("terraria-") or profile_id == "pz-rising":
            expected_keys.add("console_fifo")
        assert set(config) == expected_keys
        assert config["user"] == contract["user"]
        assert config["group"] == contract["user"]
        assert config["cwd"] == contract["cwd"]
        if "argv" in contract:
            assert config["argv"] == contract["argv"]
        assert config["environment"] == {}
        if profile_id.startswith("terraria-"):
            assert config["console_fifo"] == f"/run/game-slot/{profile_id}.console"
        assert not re.search(r"\$\{|\$\(|`|;\s*(?:sh|bash)", json.dumps(config))
    runner = (ROOT / "ops/bin/game-slot-run").read_text()
    assert "shell executables are not permitted" in runner
    assert "value[0] != \"/usr/bin/bash\"" in runner


def test_units_hardened_and_heavy_services_do_not_autostart() -> None:
    slotd = _unit("game-slotd.service")
    web = _unit("game-control-web.service")
    assert "User=root" in slotd
    for directive in ("PrivateTmp=yes", "ProtectHome=read-only", "ProtectSystem=strict", "RestrictSUIDSGID=yes"):
        assert directive in slotd
    assert "ListenStream=" not in slotd
    assert "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_FOWNER" in slotd
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
        "/opt/pzserver",
        "/home/pzuser/Zomboid",
        "/srv/game-servers/minecraft",
    )
    for path in required_rw:
        assert path in slotd
    for directive in (
        "User=gamecontrol",
        "Group=gamecontrol",
        "--host 127.0.0.1 --port 8444",
        "LoadCredential=proxy-token:/etc/game-control/secrets.d/proxy-token",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "ReadOnlyPaths=/opt/game-control/web",
        "ReadOnlyPaths=/run/game-control",
        "ReadWritePaths=/var/lib/game-control-web",
    ):
        assert directive in web
    forbidden_web_rw = ("/run/game-control", "/run/game-slot", "/var/lib/game-control ", "/var/backups/game-servers")
    rw_lines = [line for line in web.splitlines() if line.startswith("ReadWritePaths=")]
    assert all(not any(path in line for path in forbidden_web_rw) for line in rw_lines)
    assert "WantedBy=" not in web
    for name in ("terraria-vanilla.service", "terraria-tmod.service"):
        text = _unit(name)
        profile_id = name.removesuffix(".service")
        assert "ExecStart=/usr/local/libexec/game-slot-run " in text
        assert f"ExecStop=/usr/local/libexec/game-console-stop {profile_id} $MAINPID" in text
        assert "WantedBy=" not in text
        assert "Restart=on-failure" not in text


def test_installer_provisions_writable_runtime_state_directories() -> None:
    from ops.install import Installer

    paths = {path for path, _mode, _user, _group in Installer(Path("/tmp"), token_source=Path("/tmp/token")).directories()}
    assert Path("/tmp/srv/game-servers/terraria-vanilla/.local/share/Terraria") in paths
    assert Path("/tmp/srv/game-servers/terraria-tmod/.local/share/Terraria") in paths


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
    }
    assert expected <= set(content.splitlines())
    assert "d /run 0770" not in content


def test_installer_check_is_read_only_and_apply_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    token = tmp_path / "crafty-token"
    token.write_text("fixture-token\n")
    installer = ROOT / "ops" / "install.py"
    env = {**os.environ, "GAME_CONTROL_INSTALL_ROOT": str(root), "GAME_CONTROL_TOKEN_SOURCE": str(token)}
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
    assert second.returncode == 0, second.stderr
    copied = root / "etc/game-control/secrets.d/crafty-token"
    assert copied.read_text() == "fixture-token\n"
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600
    assert "fixture-token" not in apply.stdout
