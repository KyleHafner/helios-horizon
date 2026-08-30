from __future__ import annotations

import json
import os
import runpy
import stat
from pathlib import Path

import pytest


HELPER = Path(__file__).parents[1] / "ops/bin/game-sunlit-stop"
PREPARE = Path(__file__).parents[1] / "ops/bin/game-sunlit-prepare"


def _helper() -> dict[str, object]:
    return runpy.run_path(str(HELPER), run_name="game-sunlit-stop")


def test_helper_has_closed_profile_and_process_contract() -> None:
    helper = _helper()
    assert helper["PROFILE"] == "minecraft-sunlit-cobblemon"
    assert helper["METADATA"] == Path("/run/game-slot/slot.json")
    assert helper["LATEST_LOG"] == Path(
        "/srv/game-servers/minecraft-sunlit-cobblemon-current/logs/latest.log"
    )
    assert helper["FORGE_ARGS"].endswith("forge/1.20.1-47.4.0/unix_args.txt")
    assert helper["main"](["other", "123"]) == 2
    assert helper["main"](["minecraft-sunlit-cobblemon", "1"]) == 2


def test_slot_identity_requires_fixed_profile_owned_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    metadata = tmp_path / "slot.json"
    metadata.write_text(
        json.dumps(
            {
                "profile_id": "minecraft-sunlit-cobblemon",
                "pid": 4321,
                "proc_start_ticks": 99,
            }
        )
    )
    metadata.chmod(0o644)
    monkeypatch.setitem(helper["_slot_identity"].__globals__, "METADATA", metadata)
    monkeypatch.setitem(helper["_slot_identity"].__globals__, "_ticks", lambda _pid: 99)
    assert helper["_slot_identity"]("minecraft-sunlit-cobblemon", 4321) == 99
    metadata.chmod(0o666)
    with pytest.raises(ValueError, match="unsafe"):
        helper["_slot_identity"]("minecraft-sunlit-cobblemon", 4321)


def test_stop_requires_all_save_markers_and_pid_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    log = tmp_path / "latest.log"
    log.write_bytes(b"booted\n")
    log.chmod(0o640)
    globals_ = helper["_stop"].__globals__
    monkeypatch.setitem(globals_, "_latest_log", lambda: log)
    monkeypatch.setitem(globals_, "TIMEOUT_SECONDS", 1.0)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(globals_["os"], "kill", lambda pid, sig: signals.append((pid, sig)))
    ticks = iter([77, None])
    monkeypatch.setitem(globals_, "_ticks", lambda _pid: next(ticks))
    chunks = iter(
        [
            (10, b"Stopping server\nSaving players\n"),
            (20, b"Saving worlds\n"),
        ]
    )
    monkeypatch.setitem(globals_, "_read_since", lambda _path, _offset: next(chunks))
    assert helper["_stop"](4321, 77) is True
    assert len(signals) == 1


def test_read_since_uses_current_secure_regular_contract(tmp_path: Path) -> None:
    helper = _helper()
    log = tmp_path / "latest.log"
    log.write_bytes(b"booted\nready\n")
    log.chmod(0o640)
    offset, data = helper["_read_since"](log, len(b"booted\n"))
    assert offset == len(b"booted\nready\n")
    assert data == b"ready\n"


def test_latest_log_accepts_root_owned_directory_link_to_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    state = tmp_path / "state"
    logs = state / "versions/v1/logs"
    logs.mkdir(parents=True)
    latest = logs / "latest.log"
    latest.write_bytes(b"Stopping server\nSaving players\nSaving worlds\n")
    latest.chmod(0o640)
    active = tmp_path / "active"
    active.mkdir()
    (active / "logs").symlink_to(logs, target_is_directory=True)
    globals_ = helper["_latest_log"].__globals__
    monkeypatch.setitem(globals_, "LATEST_LOG", active / "logs/latest.log")
    monkeypatch.setitem(globals_, "STATE_ROOT", state)
    assert helper["_latest_log"]() == latest
    latest.unlink()
    latest.symlink_to(tmp_path / "outside")
    with pytest.raises((OSError, ValueError)):
        helper["_latest_log"]()


def test_stop_rechecks_bounded_log_tail_after_pid_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper()
    log = tmp_path / "latest.log"
    log.write_bytes(b"booted\n")
    log.chmod(0o640)
    globals_ = helper["_stop"].__globals__
    monkeypatch.setitem(globals_, "_latest_log", lambda: log)
    monkeypatch.setitem(globals_, "TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(globals_["os"], "kill", lambda _pid, _sig: None)
    monkeypatch.setitem(globals_, "_ticks", lambda _pid: None)
    monkeypatch.setitem(globals_, "_read_since", lambda _path, offset: (offset, b""))
    tails = iter([b"", b"Stopping server\nSaving players\nSaving worlds\n"])
    monkeypatch.setitem(globals_, "_read_tail", lambda _path: next(tails))
    monkeypatch.setattr(globals_["time"], "sleep", lambda _seconds: None)
    assert helper["_stop"](4321, 77) is True


def test_post_stop_mode_requires_save_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper()
    globals_ = helper["main"].__globals__
    monkeypatch.setitem(globals_, "_verify_tail", lambda: True)
    assert helper["main"](["minecraft-sunlit-cobblemon", "verify"]) == 0
    monkeypatch.setitem(globals_, "_verify_tail", lambda: False)
    assert helper["main"](["minecraft-sunlit-cobblemon", "verify"]) == 1


def test_helper_is_executable_and_unit_uses_only_fixed_transport() -> None:
    assert stat.S_IMODE(HELPER.stat().st_mode) & 0o111
    assert stat.S_IMODE(PREPARE.stat().st_mode) & 0o111
    unit = (HELPER.parents[1] / "systemd/minecraft-sunlit-cobblemon.service").read_text()
    assert "ExecStartPre=/usr/local/libexec/game-sunlit-prepare" in unit
    assert (
        "ExecStop=-/usr/local/libexec/game-sunlit-stop "
        "minecraft-sunlit-cobblemon $MAINPID"
    ) in unit
    assert (
        "ExecStopPost=/usr/local/libexec/game-sunlit-stop "
        "minecraft-sunlit-cobblemon verify"
    ) in unit
    assert "ExecStop=/usr/bin/kill" not in unit
    assert "SuccessExitStatus=130 143" in unit


def test_prepare_requires_fixed_release_and_state_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = runpy.run_path(str(PREPARE), run_name="game-sunlit-prepare")
    state = tmp_path / "state"
    releases = tmp_path / "releases"
    mutable = releases / "v1"
    active = tmp_path / "current"
    target = tmp_path / "opt/libraries"
    state.mkdir()
    mutable.mkdir(parents=True)
    active.symlink_to(mutable, target_is_directory=True)
    target.mkdir(parents=True)
    properties_path = state / "server.properties"
    properties_path.write_text("motd=Horizon\nserver-port=25565\nserver-ip=\n", encoding="utf-8")
    properties_path.chmod(0o640)
    (mutable / "server.properties").symlink_to(properties_path)
    (mutable / "libraries").symlink_to(target, target_is_directory=True)
    mutable.chmod(0o755)
    target.chmod(0o755)
    globals_ = prepare["main"].__globals__
    monkeypatch.setitem(globals_, "MUTABLE_ROOT", active)
    monkeypatch.setitem(globals_, "LINK", active / "libraries")
    monkeypatch.setitem(globals_, "TARGET", target)
    monkeypatch.setitem(globals_, "STATE_ROOT", state)
    monkeypatch.setitem(globals_, "RELEASE_ROOT", releases)
    monkeypatch.setitem(globals_, "SERVER_PROPERTIES", active / "server.properties")
    assert prepare["main"]([]) == 0
    properties = properties_path.read_text()
    assert "server-port=25566\n" in properties
    assert "server-ip=127.0.0.1\n" in properties
    assert (active / "libraries").is_symlink()
    assert os.readlink(active / "libraries") == str(target)
    assert prepare["main"]([]) == 0
    (mutable / "libraries").unlink()
    (mutable / "libraries").mkdir()
    assert prepare["main"]([]) == 2
