from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pwd
import grp
import runpy
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


RUNNER = Path(__file__).parents[1] / "ops/bin/game-slot-run"


def test_slot_acquire_retries_a_transient_inspector_lock(monkeypatch):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    calls = 0

    def flock(_fd, _flags):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert runner["_acquire_slot"](123) is True
    assert calls == 3


def test_inaccessible_optional_jvm_config_logs_sanitized_preflight_error(monkeypatch, capsys):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    original_lstat = runner["Path"].lstat

    def denied(path):
        if str(path) == "/etc/game-control/jvm/minecraft-sunlit-cobblemon.active.args":
            raise PermissionError("private deployment detail")
        return original_lstat(path)

    monkeypatch.setattr(runner["Path"], "lstat", denied)
    with pytest.raises(ValueError, match="managed JVM configuration unavailable"):
        runner["_managed_jvm_layer"]("minecraft-sunlit-cobblemon", ["/usr/bin/java"])
    assert "private deployment detail" not in capsys.readouterr().err


def test_main_reports_sanitized_managed_jvm_preflight_failure(monkeypatch, capsys):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    globals_ = runner["main"].__globals__
    monkeypatch.setitem(globals_, "_config", lambda _profile: {"argv": ["/usr/bin/java"], "cwd": "/tmp"})
    monkeypatch.setitem(globals_, "_sunlit_runtime_contract", lambda _profile, _config: None)
    monkeypatch.setitem(
        globals_,
        "_managed_jvm_layer",
        lambda _profile, _command: (_ for _ in ()).throw(
            globals_["ManagedJvmConfigurationError"]("private deployment detail")
        ),
    )
    assert runner["main"](["minecraft-sunlit-cobblemon"]) == 2
    diagnostic = capsys.readouterr().err
    assert "launch preflight failed: managed_jvm_configuration_unavailable" in diagnostic
    assert "private deployment detail" not in diagnostic


@pytest.fixture
def runner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run_dir = tmp_path / "run"
    config_dir = tmp_path / "etc" / "runner.d"
    cwd = tmp_path / "game"
    run_dir.mkdir()
    config_dir.mkdir(parents=True)
    cwd.mkdir()
    operation = run_dir / "game-slot-operation.lock"
    slot = run_dir / "slot.lock"
    metadata = run_dir / "slot.json"
    reservation = run_dir / "reservation.json"
    operation.touch()
    slot.touch()
    for path in (operation, slot, config_dir / "..", config_dir):
        os.chown(path, 0, 0)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)

    def configure(profile: str, hold_seconds: float) -> None:
        script = (
            "import time; "
            f"time.sleep({hold_seconds!r})"
        )
        target = cwd / f"{profile}-process.py"
        target.write_text(script)
        target.chmod(0o755)
        config = {
            "argv": [sys.executable, str(target)],
            "cwd": str(cwd),
            "environment": {},
            "operation_lock": str(operation),
            "slot_lock": str(slot),
            "metadata": str(metadata),
            "reservation": str(reservation),
        }
        (config_dir / f"{profile}.json").write_text(json.dumps(config))
        os.chown(config_dir / f"{profile}.json", 0, 0)
        os.chmod(config_dir / f"{profile}.json", 0o644)

    class Env:
        async def run(self, profile: str, hold_seconds: float = 0):
            configure(profile, hold_seconds)
            return await asyncio.create_subprocess_exec(
                sys.executable,
                str(RUNNER),
                profile,
                env={
                    "GAME_SLOT_RUNNER_DIR": str(config_dir),
                    "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
                    "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
                    "GAME_SLOT_RUNNER_METADATA": str(metadata),
                    "GAME_SLOT_RUNNER_RESERVATION": str(reservation),
                    "GAME_SLOT_RUNNER_TEST_MODE": "1",
                    "PATH": os.environ["PATH"],
                },
            )

    return Env()


@pytest.mark.asyncio
async def test_two_concurrent_runners_have_exactly_one_winner(runner_env):
    first, second = await asyncio.gather(
        runner_env.run("minecraft", hold_seconds=1),
        runner_env.run("pz-rising", hold_seconds=1),
    )
    first_code, second_code = await asyncio.gather(first.wait(), second.wait())
    assert sorted([first_code, second_code]) == [0, 75]


def test_lock_fd_remains_held_after_exec(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    operation.touch()
    slot.touch()
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    target = tmp_path / "target.py"
    target.write_text("import subprocess,sys; raise SystemExit(subprocess.run([sys.executable, sys.argv[1], sys.argv[2]]).returncode)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config = {
        "argv": [sys.executable, str(target), str(probe), str(slot)],
        "cwd": str(cwd),
        "operation_lock": str(operation),
        "slot_lock": str(slot),
        "metadata": str(tmp_path / "metadata.json"),
        "reservation": str(tmp_path / "reservation.json"),
        "environment": {},
    }
    (config_dir / "minecraft.json").write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(metadata),
            "GAME_SLOT_RUNNER_RESERVATION": str(tmp_path / "reservation.json"),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 0
    metadata_stat = metadata.stat()
    assert (metadata_stat.st_uid, metadata_stat.st_gid, stat.S_IMODE(metadata_stat.st_mode)) == (0, 0, 0o644)


def test_runner_exec_environment_uses_json_home_over_parent_home(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    operation.touch()
    slot.touch()
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    observed_home = tmp_path / "observed-home"
    candidate_home = "/srv/game-servers/terraria-tmod-145-ddffee7/home"
    target = cwd / "target.py"
    target.write_text(
        "import os\n"
        f"open({str(observed_home)!r}, 'w').write(os.environ['HOME'])\n"
    )
    config = {
        "argv": [sys.executable, str(target)],
        "cwd": str(cwd),
        "environment": {"HOME": candidate_home},
    }
    (config_dir / "minecraft.json").write_text(json.dumps(config))
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(metadata),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "HOME": "/systemd-like-parent-home",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 0
    assert observed_home.read_text() == candidate_home


@pytest.mark.skipif(not Path("/usr/bin/java").is_file(), reason="Java 17 launcher is unavailable")
def test_sunlit_runner_resolves_forge_relative_libraries_from_srv_cwd(tmp_path: Path):
    profile = "minecraft-sunlit-cobblemon"
    install_root = tmp_path / "opt/game-servers/minecraft-sunlit-cobblemon"
    release_root = install_root / "releases"
    release = release_root / "1.1.2-test"
    state_root = tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon-state"
    active_link = tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon-current"
    install_root.joinpath("libraries").mkdir(parents=True)
    release.mkdir(parents=True)
    state_root.joinpath(".horizon").mkdir(parents=True)
    state_root.joinpath("local").mkdir()
    release_record = state_root / ".horizon/release.json"
    release_record.write_text(json.dumps({"version": release.name}))
    release_record.chmod(0o640)
    active_link.symlink_to(release, target_is_directory=True)
    (install_root / "libraries/forge-probe.jar").write_bytes(b"not a jar")
    args_file = install_root / "unix_args.txt"
    args_file.write_text("-jar\nlibraries/forge-probe.jar\n")
    (release / "libraries").symlink_to(install_root / "libraries", target_is_directory=True)
    (release / "user_jvm_args.txt").write_text("")

    config_dir = tmp_path / "etc/runner.d"
    config_dir.mkdir(parents=True)
    config_dir.chmod(0o700)
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    operation.touch()
    slot.touch()
    (config_dir / f"{profile}.json").write_text(
        json.dumps(
            {
                "user": "svc-sunlit",
                "group": "svc-sunlit",
                "cwd": str(active_link),
                "argv": [
                    "/usr/bin/java",
                    f"-Duser.home={state_root / 'local'}",
                    f"@{active_link / 'user_jvm_args.txt'}",
                    f"@{args_file}",
                ],
                "environment": {"HOME": str(state_root / "local")},
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(RUNNER), profile],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(metadata),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "GAME_SLOT_SUNLIT_ACTIVE_LINK": str(active_link),
            "GAME_SLOT_SUNLIT_RELEASE_ROOT": str(release_root),
            "GAME_SLOT_SUNLIT_STATE_ROOT": str(state_root),
            "PATH": os.environ["PATH"],
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Invalid or corrupt jarfile" in result.stdout + result.stderr


def test_sunlit_runner_fails_closed_when_runtime_and_release_record_diverge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    release_root = tmp_path / "releases"
    release = release_root / "1.1.2-test"
    state_root = tmp_path / "state"
    active_link = tmp_path / "current"
    release.mkdir(parents=True)
    state_root.joinpath(".horizon").mkdir(parents=True)
    state_root.joinpath("local").mkdir()
    release_record = state_root / ".horizon/release.json"
    release_record.write_text(json.dumps({"version": release.name}))
    release_record.chmod(0o640)
    active_link.symlink_to(release, target_is_directory=True)
    monkeypatch.setenv("GAME_SLOT_RUNNER_TEST_MODE", "1")
    monkeypatch.setenv("GAME_SLOT_SUNLIT_ACTIVE_LINK", str(active_link))
    monkeypatch.setenv("GAME_SLOT_SUNLIT_RELEASE_ROOT", str(release_root))
    monkeypatch.setenv("GAME_SLOT_SUNLIT_STATE_ROOT", str(state_root))
    config = {
        "cwd": str(active_link),
        "argv": [
            "/usr/bin/java",
            f"-Duser.home={state_root / 'local'}",
            f"@{active_link / 'user_jvm_args.txt'}",
        ],
        "environment": {"HOME": str(state_root / "local")},
    }

    runner["_sunlit_runtime_contract"]("minecraft-sunlit-cobblemon", config)

    stale = {**config, "cwd": str(tmp_path / "legacy-tree")}
    with pytest.raises(ValueError, match="active release"):
        runner["_sunlit_runtime_contract"]("minecraft-sunlit-cobblemon", stale)

    release_record.write_text(json.dumps({"version": "older"}))
    with pytest.raises(ValueError, match="does not match"):
        runner["_sunlit_runtime_contract"]("minecraft-sunlit-cobblemon", config)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to drop to an unprivileged runner")
def test_unprivileged_runner_accepts_group_writable_common_locks(tmp_path: Path):
    # Use a world-traversable sandbox so the dropped user can reach it.
    tmp_path = Path(tempfile.mkdtemp(prefix="game-slot-runner-", dir="/tmp"))
    try:
        nobody = pwd.getpwnam("nobody")
        shared_group = grp.getgrnam("gameslot")
    except KeyError:
        pytest.skip("nobody/users accounts unavailable")
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    reservation = tmp_path / "reservation.json"
    operation.touch()
    slot.touch()
    os.chown(operation, 0, shared_group.gr_gid)
    os.chown(slot, 0, shared_group.gr_gid)
    os.chmod(operation, 0o660)
    os.chmod(slot, 0o660)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    target.chmod(0o755)
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    config = config_dir / "minecraft.json"
    config.write_text(json.dumps({"argv": ["/usr/bin/python3", str(target)], "cwd": str(cwd)}))
    os.chmod(config, 0o644)
    os.chmod(config_dir, 0o755)
    os.chmod(cwd, 0o755)
    os.chown(tmp_path, 0, shared_group.gr_gid)
    os.chmod(tmp_path, 0o770)
    wrapper = tmp_path / "invoke.py"
    wrapper.write_text(
        "import os, runpy\n"
        "from pathlib import Path\n"
        f"runner = runpy.run_path({str(RUNNER)!r}, run_name='game-slot-run')\n"
        f"g = runner['main'].__globals__\n"
        f"g['DEFAULT_CONFIG_DIR'] = Path({str(config_dir)!r})\n"
        f"g['DEFAULT_OPERATION_LOCK'] = Path({str(operation)!r})\n"
        f"g['DEFAULT_SLOT_LOCK'] = Path({str(slot)!r})\n"
        f"g['DEFAULT_METADATA'] = Path({str(metadata)!r})\n"
        f"g['DEFAULT_RESERVATION'] = Path({str(reservation)!r})\n"
        f"os.setgroups([{shared_group.gr_gid}])\n"
        f"os.setgid({nobody.pw_gid})\n"
        f"os.setuid({nobody.pw_uid})\n"
        "raise SystemExit(runner['main'](['minecraft']))\n"
    )
    result = subprocess.run(
        [sys.executable, str(wrapper)],
        env={"GAME_SLOT_RUNNER_DIR": str(config_dir), "PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0
    metadata_stat = metadata.stat()
    assert metadata_stat.st_uid == nobody.pw_uid
    assert stat.S_IMODE(metadata_stat.st_mode) == 0o644


def test_runner_lock_validation_uses_open_fd_after_inode_swap(tmp_path, monkeypatch):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    monkeypatch.setenv("GAME_SLOT_RUNNER_TEST_MODE", "1")
    lock = tmp_path / "slot.lock"
    replacement = tmp_path / "replacement"
    lock.touch()
    replacement.write_text("not a lock")
    os.chmod(lock, 0o660)
    os.chmod(replacement, 0o666)
    original_inode = lock.stat().st_ino
    real_open = runner["os"].open
    real_fstat = runner["os"].fstat
    fstat_calls = []

    def open_and_swap(path, flags, *args):
        fd = real_open(path, flags, *args)
        if Path(path) == lock:
            os.replace(replacement, lock)
        return fd

    def record_fstat(fd):
        fstat_calls.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(runner["os"], "open", open_and_swap)
    monkeypatch.setattr(runner["os"], "fstat", record_fstat)
    fd = runner["_open_secure_lock"](lock, os.O_RDWR)
    try:
        assert real_fstat(fd).st_ino == original_inode
        assert fstat_calls == [fd]
    finally:
        os.close(fd)


def test_runner_uses_canonical_slot_lock_path():
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    assert runner["DEFAULT_SLOT_LOCK"] == Path("/run/game-control/slot.lock")
    assert runner["DEFAULT_OPERATION_LOCK"] == Path("/run/game-control/operation.lock")
    assert runner["DEFAULT_RESERVATION"] == Path("/run/game-control/reservation.json")


def test_runner_accepts_candidate_profile_reservation(tmp_path: Path):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    reservation = tmp_path / "reservation.json"
    reservation.write_text(
        json.dumps(
            {
                "profile_id": "terraria-tmod-145-candidate",
                "operation_id": "op",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": runner["_ticks"](os.getpid()),
                "expires_at": time.time() + 10,
            }
        )
    )

    assert runner["_profile"]("terraria-tmod-145-candidate") == "terraria-tmod-145-candidate"
    assert runner["_reservation_status"](
        reservation, "terraria-tmod-145-candidate"
    ) is True
    with pytest.raises(ValueError, match="invalid profile id"):
        runner["_profile"]("unknown-profile")


def test_runner_prepares_fixed_terraria_console_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    monkeypatch.setenv("GAME_SLOT_RUNNER_TEST_MODE", "1")
    monkeypatch.setenv("GAME_SLOT_RUNNER_CONSOLE_DIR", str(tmp_path))
    path = tmp_path / "terraria-vanilla.console"
    resolved = runner["_console_path"](str(path), "terraria-vanilla")
    fd = runner["_prepare_console"](resolved)
    try:
        writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(writer, b"save\n")
        finally:
            os.close(writer)
        assert os.read(fd, 5) == b"save\n"
        info = path.lstat()
        assert stat.S_ISFIFO(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def test_terraria_runner_inherits_blocking_console_stdin(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    reservation = tmp_path / "reservation.json"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    output = tmp_path / "stdin.txt"
    target = cwd / "target.py"
    target.write_text(
        "import pathlib,sys\n"
        "line = sys.stdin.readline()\n"
        f"pathlib.Path({str(output)!r}).write_text(line)\n"
    )
    console_dir = tmp_path / "console"
    console_dir.mkdir()
    console_fifo = console_dir / "terraria-vanilla.console"
    (config_dir / "terraria-vanilla.json").write_text(
        json.dumps(
            {
                "user": "terraria-vanilla",
                "group": "terraria-vanilla",
                "cwd": str(cwd),
                "console_fifo": str(console_fifo),
                "argv": [sys.executable, str(target)],
                "environment": {},
            }
        )
    )
    env = {
        "GAME_SLOT_RUNNER_DIR": str(config_dir),
        "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
        "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
        "GAME_SLOT_RUNNER_METADATA": str(metadata),
        "GAME_SLOT_RUNNER_RESERVATION": str(reservation),
        "GAME_SLOT_RUNNER_CONSOLE_DIR": str(console_dir),
        "GAME_SLOT_RUNNER_TEST_MODE": "1",
        "PATH": os.environ["PATH"],
    }
    process = subprocess.Popen([sys.executable, str(RUNNER), "terraria-vanilla"], env=env)
    fifo = console_fifo
    try:
        deadline = time.monotonic() + 5
        while not fifo.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fifo.exists()
        assert process.poll() is None
        writer = os.open(fifo, os.O_WRONLY)
        try:
            os.write(writer, b"ready\n")
        finally:
            os.close(writer)
        assert process.wait(timeout=5) == 0
        assert output.read_text() == "ready\n"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        fifo.unlink(missing_ok=True)


def test_runner_rejects_noncanonical_console_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    monkeypatch.setenv("GAME_SLOT_RUNNER_TEST_MODE", "1")
    monkeypatch.setenv("GAME_SLOT_RUNNER_CONSOLE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        runner["_console_path"](str(tmp_path / "other.console"), "terraria-vanilla")
    with pytest.raises(ValueError):
        runner["_console_path"](str(tmp_path / "minecraft.console"), "minecraft")


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root for lock ownership checks")
def test_runner_rejects_noncanonical_lock_ownership_and_mode(tmp_path):
    try:
        gameslot_gid = grp.getgrnam("gameslot").gr_gid
        nobody_uid = pwd.getpwnam("nobody").pw_uid
    except KeyError:
        pytest.skip("gameslot/nobody accounts unavailable")
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")

    def checked(path, uid, gid, mode):
        path.touch()
        os.chown(path, uid, gid)
        os.chmod(path, mode)
        with pytest.raises(ValueError):
            runner["_open_secure_lock"](path, os.O_RDWR)

    checked(tmp_path / "wrong-owner", nobody_uid, gameslot_gid, 0o660)
    checked(tmp_path / "wrong-group", 0, os.getgid(), 0o660)
    checked(tmp_path / "wrong-mode", 0, gameslot_gid, 0o640)
    checked(tmp_path / "operation-wrong-mode", 0, gameslot_gid, 0o600)
    linked = tmp_path / "linked"
    linked.touch()
    os.chown(linked, 0, gameslot_gid)
    os.chmod(linked, 0o660)
    os.link(linked, tmp_path / "linked-alias")
    with pytest.raises(ValueError):
        runner["_open_secure_lock"](linked, os.O_RDWR)
    correct = tmp_path / "correct"
    correct.touch()
    os.chown(correct, 0, gameslot_gid)
    os.chmod(correct, 0o660)
    fd = runner["_open_secure_lock"](correct, os.O_RDWR)
    os.close(fd)


def test_runner_rejects_live_reservation_for_another_profile(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    reservation = tmp_path / "reservation.json"
    reservation.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": _start_ticks(os.getpid()),
                "expires_at": time.time() + 10,
            }
        )
    )
    (config_dir / "pz-rising.json").write_text(
        json.dumps(
            {
                "argv": [sys.executable, str(target)],
                "cwd": str(cwd),
                "operation_lock": str(operation),
                "slot_lock": str(slot),
                "metadata": str(tmp_path / "metadata.json"),
                "reservation": str(reservation),
                "environment": {},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "pz-rising"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_RESERVATION": str(reservation),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 75


def test_runner_rejects_shell_command_from_root_config(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    (config_dir / "minecraft.json").write_text(
        json.dumps(
            {
                "argv": ["/bin/sh", "-c", "exit 0"],
                "cwd": str(cwd),
                "operation_lock": str(operation),
                "slot_lock": str(slot),
                "metadata": str(tmp_path / "metadata.json"),
                "reservation": str(tmp_path / "reservation.json"),
                "environment": {},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(tmp_path / "reservation.json"),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 2


def test_runner_rejects_malformed_root_config(tmp_path: Path):
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    (config_dir / "minecraft.json").write_text("{}")
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 2


def test_runner_rejects_path_overrides_without_root_test_mode(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    (config_dir / "minecraft.json").write_text(
        json.dumps({"argv": [sys.executable, str(target)], "cwd": str(cwd)})
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(tmp_path / "reservation.json"),
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "reservation",
    [
        {
            "profile_id": "minecraft",
            "state_generation": 1,
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": 4_000_000_000,
        },
        {
            "profile_id": "minecraft",
            "operation_id": "op",
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": 4_000_000_000,
        },
        {
            "profile_id": "minecraft",
            "operation_id": "op",
            "state_generation": 1,
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": float("nan"),
        },
        {
            "profile_id": "minecraft",
            "operation_id": "op",
            "state_generation": 1,
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": float("inf"),
        },
        {
            "profile_id": "minecraft",
            "operation_id": "op",
            "state_generation": 1,
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": 10**1000,
        },
        {
            "profile_id": "minecraft",
            "operation_id": "op",
            "state_generation": 1,
            "controller_pid": os.getpid(),
            "controller_start_ticks": 1,
            "expires_at": 4_000_000_000,
        },
    ],
)
def test_runner_ignores_invalid_or_far_future_reservation(tmp_path: Path, reservation):
    reservation = dict(reservation)
    reservation["controller_pid"] = os.getpid()
    reservation["controller_start_ticks"] = _start_ticks(os.getpid())
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    reservation_path = tmp_path / "reservation.json"
    reservation_path.write_text(json.dumps(reservation, allow_nan=True))
    (config_dir / "pz-rising.json").write_text(
        json.dumps({"argv": [sys.executable, str(target)], "cwd": str(cwd)})
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "pz-rising"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(reservation_path),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 0


def test_runner_rejects_update_reservation_even_for_matching_profile(tmp_path: Path):
    runner = runpy.run_path(str(RUNNER), run_name="game-slot-run")
    reservation_path = tmp_path / "reservation.json"
    reservation_path.write_text(json.dumps({
        "profile_id": "minecraft-sunlit-cobblemon",
        "operation_id": "sunlit-update-op",
        "state_generation": 0,
        "controller_pid": os.getpid(),
        "controller_start_ticks": _start_ticks(os.getpid()),
        "expires_at": time.time() + 10,
        "operation_kind": "update",
    }))
    assert runner["_reservation_status"](reservation_path, "minecraft-sunlit-cobblemon") is False


def test_direct_start_waits_for_controller_reservation_commit(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    reservation = tmp_path / "reservation.json"
    (config_dir / "pz-rising.json").write_text(
        json.dumps({"argv": [sys.executable, str(target)], "cwd": str(cwd)})
    )
    writer_fd = os.open(operation, os.O_RDWR)
    fcntl.flock(writer_fd, fcntl.LOCK_EX)
    direct = subprocess.Popen(
        [sys.executable, str(RUNNER), "pz-rising"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(reservation),
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    time.sleep(0.05)
    reservation.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": _start_ticks(os.getpid()),
                "expires_at": time.time() + 10,
            }
        )
    )
    fcntl.flock(writer_fd, fcntl.LOCK_UN)
    os.close(writer_fd)
    assert direct.wait(timeout=2) == 75


def test_target_unit_direct_start_requires_controller_reservation(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    (config_dir / "minecraft.json").write_text(
        json.dumps({"argv": [sys.executable, str(target)], "cwd": str(cwd)})
    )
    writer_fd = os.open(operation, os.O_RDWR)
    fcntl.flock(writer_fd, fcntl.LOCK_EX)
    fcntl.flock(writer_fd, fcntl.LOCK_UN)
    os.close(writer_fd)
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(tmp_path / "reservation.json"),
            "GAME_SLOT_REQUIRE_RESERVATION": "1",
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 75


def test_target_unit_accepts_matching_live_controller_reservation(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    operation.touch()
    slot.touch()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    target = cwd / "target.py"
    target.write_text("raise SystemExit(0)")
    config_dir = tmp_path / "runner.d"
    config_dir.mkdir()
    (config_dir / "minecraft.json").write_text(
        json.dumps({"argv": [sys.executable, str(target)], "cwd": str(cwd)})
    )
    reservation = tmp_path / "reservation.json"
    reservation.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": _start_ticks(os.getpid()),
                "expires_at": time.time() + 10,
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(RUNNER), "minecraft"],
        env={
            "GAME_SLOT_RUNNER_DIR": str(config_dir),
            "GAME_SLOT_RUNNER_OPERATION_LOCK": str(operation),
            "GAME_SLOT_RUNNER_SLOT_LOCK": str(slot),
            "GAME_SLOT_RUNNER_METADATA": str(tmp_path / "metadata.json"),
            "GAME_SLOT_RUNNER_RESERVATION": str(reservation),
            "GAME_SLOT_REQUIRE_RESERVATION": "1",
            "GAME_SLOT_RUNNER_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 0


def _start_ticks(pid: int) -> int:
    _, rest = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)
    return int(rest.split()[19])
