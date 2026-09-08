import os
from pathlib import Path
import tomllib

import pytest

from game_control.schedule_config import ScheduleConfigError, load_schedule_entries, write_schedule_config


def test_schedule_config_replacement_is_atomic_and_keeps_a_backup(tmp_path: Path):
    path = tmp_path / "game-control.toml"
    original = (
        'profiles_dir = "profiles.d"\n'
        '[[schedule]]\n'
        'cron = "0 20 * * 5"\n'
        'profile = "minecraft"\n'
        'enabled = false\n\n'
        '[crafty]\n'
        'verify = false\n'
    )
    path.write_text(original, encoding="utf-8")

    write_schedule_config(path, [{"cron": "1 * * * *", "profile": "pz-rising", "enabled": False}])

    updated = path.read_text(encoding="utf-8")
    assert 'cron = "1 * * * *"' in updated
    assert 'profile = "pz-rising"' in updated
    assert "enabled = false" in updated
    assert tomllib.loads(updated)["schedule"][0]["enabled"] is False
    assert 'cron = "0 20 * * 5"' not in updated
    assert updated.index('profiles_dir =') < updated.index('[crafty]')
    backups = list(tmp_path.glob("game-control.toml.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_schedule_config_rejects_invalid_enabled_without_writing(tmp_path: Path):
    path = tmp_path / "game-control.toml"
    original = 'profiles_dir = "profiles.d"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ScheduleConfigError, match="invalid schedule enabled"):
        write_schedule_config(path, [{"cron": "* * * * *", "profile": "minecraft", "enabled": "false"}])

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.bak")) == []


def test_schedule_config_rejects_invalid_cron_without_writing(tmp_path: Path):
    path = tmp_path / "game-control.toml"
    original = 'profiles_dir = "profiles.d"\n'
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ScheduleConfigError, match="no fire time within the next four years"):
        write_schedule_config(path, [{"cron": "0 0 31 2 *", "profile": "minecraft"}])

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.bak")) == []


def test_schedule_config_rejects_invalid_backup_destination(tmp_path: Path):
    path = tmp_path / "game-control.toml"
    path.write_text('profiles_dir = "profiles.d"\n', encoding="utf-8")

    with pytest.raises(ScheduleConfigError, match="invalid backup destination"):
        write_schedule_config(
            path,
            [{
                "cron": "0 3 * * *",
                "profile": "minecraft-sunlit-cobblemon",
                "backup_destination": "arbitrary",
            }],
        )


def test_schedule_config_removes_commented_headers_and_preserves_multiline_text(tmp_path: Path):
    path = tmp_path / "game-control.toml"
    original = (
        'description = "[[schedule]] in a normal string"\n'
        'multiline = """text\n'
        '[[schedule]]\n'
        'still text"""\n'
        '[[schedule]] # old entry\n'
        'cron = "0 3 * * *"\n'
        'profile = "minecraft"\n'
        '[crafty]\nverify = false\n'
    )
    path.write_text(original, encoding="utf-8")

    write_schedule_config(path, [])

    updated = path.read_text(encoding="utf-8")
    assert "[[schedule]] # old entry" not in updated
    assert "[[schedule]] in a normal string" in updated
    assert "still text\"\"\"" in updated
    assert tomllib.loads(updated)["crafty"]["verify"] is False
    assert "schedule" not in tomllib.loads(updated)

    write_schedule_config(path, [{"cron": "1 * * * *", "profile": "pz-rising"}])
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["schedule"][0]["profile"] == "pz-rising"
    assert parsed["multiline"] == "text\n[[schedule]]\nstill text"


def test_schedule_override_can_be_created_empty_and_persists(tmp_path: Path):
    override = tmp_path / "schedules.toml"
    config = {"schedule": [{"cron": "0 3 * * *", "profile": "minecraft"}]}

    write_schedule_config(override, [])

    assert override.exists()
    assert load_schedule_entries(config, override) == []
    assert load_schedule_entries(config, override) == []


def test_malformed_schedule_override_fails_closed(tmp_path: Path):
    override = tmp_path / "schedules.toml"
    override.write_text("[[schedule]\ncron =", encoding="utf-8")

    with pytest.raises(ScheduleConfigError, match="invalid schedule override"):
        load_schedule_entries({"schedule": []}, override)


def test_schedule_override_dangling_link_and_special_file_fail_closed(tmp_path: Path):
    dangling = tmp_path / "dangling.toml"
    dangling.symlink_to(tmp_path / "missing.toml")
    with pytest.raises(ScheduleConfigError, match="schedule override is unavailable"):
        load_schedule_entries({"schedule": []}, dangling)

    special = tmp_path / "special.toml"
    os.mkfifo(special)
    with pytest.raises(ScheduleConfigError, match="schedule override is unavailable"):
        load_schedule_entries({"schedule": []}, special)
