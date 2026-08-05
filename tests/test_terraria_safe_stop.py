from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
STOPPER = ROOT / "ops/bin/game-console-stop"


def test_safe_stop_sends_save_before_exit_and_waits_for_server(tmp_path: Path):
    console_dir = tmp_path / "console"
    console_dir.mkdir(mode=0o770)
    fifo = console_dir / "terraria-vanilla.console"
    os.mkfifo(fifo, 0o600)
    keeper = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    os.set_blocking(keeper, True)
    received = tmp_path / "received.txt"
    child_code = (
        "import pathlib,sys,time; "
        "lines=[sys.stdin.readline().strip(),sys.stdin.readline().strip()]; "
        "pathlib.Path(sys.argv[1]).write_text('\\n'.join(lines)+'\\n'); "
        "time.sleep(0.35)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(received)],
        stdin=keeper,
        pass_fds=(keeper,),
    )
    try:
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(STOPPER), "terraria-vanilla", str(child.pid)],
            env={
                "GAME_CONSOLE_STOP_DIR": str(console_dir),
                "GAME_CONSOLE_STOP_TEST_MODE": "1",
                "GAME_CONSOLE_STOP_TIMEOUT": "5",
                "PATH": os.environ["PATH"],
            },
            timeout=10,
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 0
        assert elapsed >= 0.25
        assert child.poll() == 0
        assert received.read_text() == "save\nexit\n"
        assert not fifo.exists()
    finally:
        os.close(keeper)
        if child.poll() is None:
            child.kill()
            child.wait()


def test_safe_stop_rejects_unknown_profile(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(STOPPER), "unknown-profile", str(os.getpid())],
        env={
            "GAME_CONSOLE_STOP_DIR": str(tmp_path),
            "GAME_CONSOLE_STOP_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )
    assert result.returncode == 2


def test_safe_stop_accepts_candidate_profile_without_console(tmp_path: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(STOPPER),
            "terraria-tmod-145-candidate",
            str(os.getpid()),
        ],
        env={
            "GAME_CONSOLE_STOP_DIR": str(tmp_path),
            "GAME_CONSOLE_STOP_TEST_MODE": "1",
            "PATH": os.environ["PATH"],
        },
    )

    assert result.returncode == 0


def test_safe_stop_missing_fifo_is_success_for_legacy_process(tmp_path: Path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        result = subprocess.run(
            [sys.executable, str(STOPPER), "terraria-vanilla", str(child.pid)],
            env={
                "GAME_CONSOLE_STOP_DIR": str(tmp_path),
                "GAME_CONSOLE_STOP_TEST_MODE": "1",
                "PATH": os.environ["PATH"],
            },
            timeout=2,
        )
        assert result.returncode == 0
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_safe_stop_stale_fifo_is_success_without_a_reader(tmp_path: Path):
    fifo = tmp_path / "terraria-tmod.console"
    os.mkfifo(fifo, 0o600)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        result = subprocess.run(
            [sys.executable, str(STOPPER), "terraria-tmod", str(child.pid)],
            env={
                "GAME_CONSOLE_STOP_DIR": str(tmp_path),
                "GAME_CONSOLE_STOP_TEST_MODE": "1",
                "PATH": os.environ["PATH"],
            },
            timeout=2,
        )
        assert result.returncode == 0
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)
