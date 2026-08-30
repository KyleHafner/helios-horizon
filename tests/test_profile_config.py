from pathlib import Path
import os
import stat
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from game_control.models import HealthState, ObservedState, ProfileId
from game_control.profile_config import ConfigValidationError, get_profile_config, set_profile_config
from game_control.controller import Controller
from game_control.protocol import GetProfileConfig, ProfileStatus, RpcRequest, RpcSuccess, SetProfileConfig, StatusSnapshot


def profile(tmp_path: Path, profile_id: ProfileId):
    return SimpleNamespace(id=profile_id, paths=SimpleNamespace(mutable_root=tmp_path, config_history_root=tmp_path / ".history"))


def test_minecraft_config_is_typed_whitelisted_and_secret_safe(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Hello\nmax-players=8\npvp=true\n", encoding="utf-8")

    result = get_profile_config(profile(tmp_path, ProfileId.MINECRAFT))

    assert {item["key"] for item in result} == {"motd", "max-players", "view-distance", "difficulty", "pvp", "white-list"}
    assert next(item for item in result if item["key"] == "motd")["value"] == "Hello"
    with pytest.raises(ConfigValidationError):
        set_profile_config(profile(tmp_path, ProfileId.MINECRAFT), {"unknown": 1})
    with pytest.raises(ConfigValidationError):
        set_profile_config(profile(tmp_path, ProfileId.MINECRAFT), {"motd": "bad\nvalue"})
    with pytest.raises(ConfigValidationError):
        set_profile_config(profile(tmp_path, ProfileId.MINECRAFT), {"max-players": 65})


def test_sunlit_cobblemon_uses_the_minecraft_config_contract(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Sunlit\nmax-players=10\n", encoding="utf-8")

    result = get_profile_config(profile(tmp_path, ProfileId.MINECRAFT_SUNLIT_COBBLEMON))

    assert next(item for item in result if item["key"] == "motd")["value"] == "Sunlit"
    assert next(item for item in result if item["key"] == "max-players")["value"] == 10


def test_set_is_atomic_backs_up_and_reports_restart_required(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Before\nmax-players=8\n", encoding="utf-8")
    os.chmod(config, 0o640)

    changed = set_profile_config(profile(tmp_path, ProfileId.MINECRAFT), {"motd": "After", "max-players": 12})

    assert config.read_text(encoding="utf-8").startswith("motd=After\nmax-players=12\n")
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    backups = list((tmp_path / ".history" / "minecraft").glob("server.properties.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "motd=Before\nmax-players=8\n"
    assert changed["changed"] == ["max-players", "motd"]
    assert changed["restart_required"] == ["max-players", "motd"]


def test_config_path_symlink_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside.properties"
    outside.write_text("motd=outside\n", encoding="utf-8")
    (tmp_path / "server.properties").symlink_to(outside)
    with pytest.raises(ConfigValidationError, match="outside"):
        get_profile_config(profile(tmp_path, ProfileId.MINECRAFT))


def test_history_destination_symlink_cannot_overwrite_external_file(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Before\n", encoding="utf-8")
    history = tmp_path / "history"
    history.mkdir(mode=0o700)
    history_profile = history / "minecraft"
    history_profile.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    item = profile(tmp_path, ProfileId.MINECRAFT)
    item.paths.config_history_root = history
    set_profile_config(item, {"motd": "After"})
    assert victim.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob("*.bak"))
    assert list(history_profile.glob("*.bak"))


def test_terraria_password_is_write_only(tmp_path: Path):
    config = tmp_path / "config" / "serverconfig.txt"
    config.parent.mkdir()
    config.write_text("maxplayers=8\npassword=secret\nsecure=true\n", encoding="utf-8")
    item = next(item for item in get_profile_config(profile(tmp_path, ProfileId.TERRARIA_VANILLA)) if item["key"] == "password")
    assert item["value"] is None
    assert item["configured"] is True
    set_profile_config(profile(tmp_path, ProfileId.TERRARIA_VANILLA), {"password": "new-secret"})
    assert "new-secret" in config.read_text(encoding="utf-8")


async def test_controller_config_rpc_audits_and_reports_restart(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Before\n", encoding="utf-8")
    item = profile(tmp_path, ProfileId.MINECRAFT)
    controller = Controller.for_testing(tmp_path)
    controller.profiles = {ProfileId.MINECRAFT: item}
    controller.services = SimpleNamespace(status=SimpleNamespace(snapshot=lambda *_args, **_kwargs: StatusSnapshot(
        generation=0,
        observed_at=datetime.now(timezone.utc),
        profiles=(ProfileStatus(
            profile_id=ProfileId.MINECRAFT, state=ObservedState.STOPPED,
            health=HealthState.UNKNOWN, slot_owner=None, active_job_id=None,
            pid=None, started_at=None, uptime_seconds=None, cpu_percent=None,
            rss_bytes=None, players_online=0, installed_version=None,
            restart_required=False, required_ports_ready=False,
        ),),
    )))

    response = await controller.execute(RpcRequest(request_id=uuid4(), actor="operator", action=SetProfileConfig(kind="set_profile_config", profile_id=ProfileId.MINECRAFT, changes={"motd": "After"})))

    assert isinstance(response, RpcSuccess)
    assert response.result.restart_required == ("motd",)
    assert controller.state_db.connection.execute("SELECT code FROM events").fetchone()[0] == "config_changed"
    assert controller.state_db.connection.execute("SELECT action FROM audit").fetchone()[0] == "set_profile_config"
