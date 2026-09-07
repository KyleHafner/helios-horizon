import asyncio
import json
import os
from pathlib import Path
import threading

import pytest

from game_control.log_follower import (
    LineTooLargeError,
    LogFollower,
    LogFollowerError,
    UnsafeLogPathError,
)


@pytest.mark.asyncio
async def test_async_cursor_save_does_not_block_event_loop(tmp_path, monkeypatch):
    path = tmp_path / "game.log"
    checkpoint = tmp_path / "offset.json"
    path.write_text("ready\n")
    entered = threading.Event()
    release = threading.Event()

    follower = LogFollower(path, checkpoint)

    def slow_save():
        entered.set()
        release.wait(2)

    monkeypatch.setattr(follower, "_save", slow_save)
    task = asyncio.create_task(follower.follow_async())
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_async_cursor_save_drains_in_flight_save_on_cancellation(tmp_path, monkeypatch):
    path = tmp_path / "game.log"
    path.write_text("ready\n")
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    follower = LogFollower(path, tmp_path / "offset.json")

    def slow_save():
        entered.set()
        release.wait(2)
        completed.set()

    monkeypatch.setattr(follower, "_save", slow_save)
    task = asyncio.create_task(follower.follow_async())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not completed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


@pytest.mark.asyncio
async def test_concurrent_follow_calls_are_serialized(tmp_path, monkeypatch):
    path = tmp_path / "game.log"
    path.write_text("ready\n")
    entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    follower = LogFollower(path, tmp_path / "offset.json")

    def slow_save():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        release.wait(2)
        active -= 1

    monkeypatch.setattr(follower, "_save", slow_save)
    first = asyncio.create_task(follower.follow_async())
    assert await asyncio.to_thread(entered.wait, 1)
    second = asyncio.create_task(follower.follow_async())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    first_events, second_events = await asyncio.gather(first, second)
    assert [event.line for event in first_events] == ["ready"]
    assert second_events == ()
    assert max_active == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_cursor_save_before_retry(tmp_path, monkeypatch):
    path = tmp_path / "game.log"
    checkpoint = tmp_path / "offset.json"
    path.write_text("ready\n")
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    follower = LogFollower(path, checkpoint)
    original_save = follower._save

    def slow_save():
        entered.set()
        release.wait(2)
        original_save()
        completed.set()

    monkeypatch.setattr(follower, "_save", slow_save)
    task = asyncio.create_task(follower.follow_async())
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not completed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()

    retry = await follower.follow_async()
    assert retry == ()
    assert await LogFollower(path, checkpoint).follow_async() == ()


@pytest.mark.asyncio
async def test_callback_cancellation_resets_uncommitted_cursor_state(tmp_path):
    path = tmp_path / "game.log"
    checkpoint = tmp_path / "offset.json"
    path.write_text("ready\n")
    follower = LogFollower(path, checkpoint)

    async def cancel(_event):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await follower.follow_async(cancel)

    events = await follower.follow_async()
    assert [event.line for event in events] == ["ready"]


def test_append_and_exact_once_with_persistent_offset(tmp_path: Path):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("one\n")
    seen = []
    follower = LogFollower(path, checkpoint, sanitizer=lambda text: text.replace("secret", "[redacted]"))
    assert [e.line for e in follower.follow(seen.append)] == ["one"]
    assert follower.follow(seen.append) == ()
    path.write_text("one\ntwo secret\n")
    assert [e.line for e in follower.follow(seen.append)] == ["two [redacted]"]
    assert len(seen) == 2
    assert "secret" not in checkpoint.read_text()


def test_partial_line_is_buffered_and_restart_replays_only_when_complete(tmp_path: Path):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_bytes(b"partial")
    follower = LogFollower(path, checkpoint)
    assert follower.follow() == ()
    assert not checkpoint.exists()
    path.write_bytes(b"partial done\n")
    assert [e.line for e in follower.follow()] == ["partial done"]
    assert [e.line for e in LogFollower(path, checkpoint).follow()] == []


def test_rotation_and_truncation_emit_explicit_reset_markers(tmp_path: Path):
    path = tmp_path / "server.log"
    path.write_text("old\n")
    follower = LogFollower(path)
    assert [e.kind for e in follower.follow()] == ["line"]
    replacement = tmp_path / "replacement.log"
    replacement.write_text("new\n")
    os.replace(replacement, path)
    events = follower.follow()
    assert [(e.kind, e.reason) for e in events] == [("reset", "rotation"), ("line", None)]
    path.write_text("x\n")
    events = follower.follow()
    assert events[0].kind == "reset"
    assert events[0].reason == "truncation"


def test_read_and_line_limits_are_enforced(tmp_path: Path):
    path = tmp_path / "server.log"
    path.write_bytes(b"x" * 9 + b"\n")
    with pytest.raises(LineTooLargeError):
        LogFollower(path, max_line_bytes=8).follow()
    path.write_bytes(b"x\n" * 20)
    assert len(LogFollower(path, max_read_bytes=3).follow()) <= 2
    with pytest.raises(LogFollowerError):
        LogFollower(path, max_file_bytes=2).follow()


def test_symlink_and_nonregular_paths_are_refused(tmp_path: Path):
    target = tmp_path / "target"
    target.write_text("line\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(UnsafeLogPathError):
        LogFollower(link).follow()
    with pytest.raises(UnsafeLogPathError):
        LogFollower(tmp_path).follow()


def test_checkpoint_restores_epoch_and_offset_without_raw_text(tmp_path: Path):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("first event\n")
    LogFollower(path, checkpoint).follow()
    payload = json.loads(checkpoint.read_text())
    assert set(payload) == {"schema_version", "device", "inode", "offset"}
    assert "event" not in checkpoint.read_text()
    path.write_text("first event\nsecond\n")
    assert [e.line for e in LogFollower(path, checkpoint).follow()] == ["second"]


def test_callback_failure_does_not_advance_failed_line(tmp_path: Path):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("one\ntwo\n")
    calls = []

    def fail_once(event):
        calls.append(event.line)
        if event.line == "two" and calls.count("two") == 1:
            raise RuntimeError("downstream unavailable")

    follower = LogFollower(path, checkpoint)
    with pytest.raises(RuntimeError):
        follower.follow(fail_once)
    assert calls == ["one", "two"]
    assert [event.line for event in follower.follow(fail_once)] == ["two"]


def test_replacement_race_is_refused_before_delivery(tmp_path: Path, monkeypatch):
    path = tmp_path / "server.log"
    path.write_text("line\n")
    original = Path.lstat
    calls = 0

    def race(current):
        nonlocal calls
        calls += 1
        result = original(current)
        if calls == 2:
            replacement = tmp_path / "replacement"
            replacement.write_text("other\n")
            os.replace(replacement, path)
        return result

    monkeypatch.setattr(Path, "lstat", race)
    with pytest.raises(UnsafeLogPathError):
        LogFollower(path).follow()


def test_partial_line_truncate_compares_read_cursor_and_restarts_cleanly(tmp_path: Path):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_bytes(b"partial line that is not complete")
    follower = LogFollower(path, checkpoint)
    assert follower.follow() == ()
    path.write_bytes(b"new\n")
    events = follower.follow()
    assert [(event.kind, event.reason, event.line) for event in events] == [("reset", "truncation", None), ("line", None, "new")]


def test_checkpoint_symlink_and_insecure_parent_fail_closed(tmp_path: Path):
    path = tmp_path / "server.log"
    path.write_text("line\n")
    target = tmp_path / "target.json"
    target.write_text("{}")
    checkpoint = tmp_path / "offset.json"
    checkpoint.symlink_to(target)
    with pytest.raises(LogFollowerError):
        LogFollower(path, checkpoint).follow()
    checkpoint.unlink()
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(LogFollowerError):
        LogFollower(path, insecure / "offset.json").follow()


def test_checkpoint_mode_is_0600_and_directory_is_fsynced(tmp_path: Path, monkeypatch):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("line\n")
    fsync_fds = []
    original_fsync = os.fsync

    def record(fd):
        fsync_fds.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    LogFollower(path, checkpoint).follow()
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert len(fsync_fds) >= 2


def test_checkpoint_parent_wrong_owner_fails_closed(tmp_path: Path, monkeypatch):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("line\n")
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(LogFollowerError, match="ownership"):
        LogFollower(path, checkpoint).follow()


def test_checkpoint_read_open_race_is_rejected(tmp_path: Path, monkeypatch):
    path, checkpoint = tmp_path / "server.log", tmp_path / "offset.json"
    path.write_text("line\n")
    LogFollower(path, checkpoint).follow()
    original_open = os.open
    replaced = False

    def race(current, flags, *args):
        nonlocal replaced
        fd = original_open(current, flags, *args)
        if current == checkpoint and not replaced:
            replaced = True
            replacement = tmp_path / "replacement.json"
            replacement.write_text(checkpoint.read_text())
            replacement.chmod(0o600)
            os.replace(replacement, checkpoint)
        return fd

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(LogFollowerError):
        LogFollower(path, checkpoint).follow()
