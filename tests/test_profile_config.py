from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from game_control.models import ProfileId
from game_control.profile_config import ConfigValidationError, get_profile_config, set_profile_config
from game_control.controller import Controller
from game_control.protocol import GetProfileConfig, RpcRequest, RpcSuccess, SetProfileConfig


def profile(tmp_path: Path, profile_id: ProfileId):
    return SimpleNamespace(id=profile_id, paths=SimpleNamespace(mutable_root=tmp_path))


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


def test_set_is_atomic_backs_up_and_reports_restart_required(tmp_path: Path):
    config = tmp_path / "server.properties"
    config.write_text("motd=Before\nmax-players=8\n", encoding="utf-8")

    changed = set_profile_config(profile(tmp_path, ProfileId.MINECRAFT), {"motd": "After", "max-players": 12})

    assert config.read_text(encoding="utf-8").startswith("motd=After\nmax-players=12\n")
    backups = list(tmp_path.glob("server.properties.*.bak"))
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

    response = await controller.execute(RpcRequest(request_id=uuid4(), actor="operator", action=SetProfileConfig(kind="set_profile_config", profile_id=ProfileId.MINECRAFT, changes={"motd": "After"})))

    assert isinstance(response, RpcSuccess)
    assert response.result.restart_required == ("motd",)
    assert controller.state_db.connection.execute("SELECT code FROM events").fetchone()[0] == "config_changed"
    assert controller.state_db.connection.execute("SELECT action FROM audit").fetchone()[0] == "set_profile_config"
