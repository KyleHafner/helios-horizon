from __future__ import annotations

import json
import grp
import os
import stat
import threading
from pathlib import Path

import pytest

import game_control.slot as slot_module
from game_control.slot import ReservationStore, SlotInspector
from game_control.models import ProfileId


def test_canonical_slot_lock_is_outside_metadata_directory():
    assert slot_module.SLOT_LOCK == Path("/run/game-control/slot.lock")
    assert slot_module.OPERATION_LOCK == Path("/run/game-control/operation.lock")
    assert slot_module.RESERVATION_FILE == Path("/run/game-control/reservation.json")
    assert slot_module.SLOT_METADATA == Path("/run/game-slot/slot.json")
    assert stat.S_IMODE(os.stat("/run").st_mode) & 0o020 == 0


def test_slot_lock_validation_uses_open_fd_after_inode_swap(tmp_path, monkeypatch):
    lock = tmp_path / "slot.lock"
    replacement = tmp_path / "replacement"
    lock.touch()
    replacement.write_text("not a lock")
    os.chmod(lock, 0o644)
    os.chmod(replacement, 0o666)
    original_inode = lock.stat().st_ino
    real_open = slot_module.os.open
    real_fstat = slot_module.os.fstat
    fstat_calls = []

    def open_and_swap(path, flags, *args):
        fd = real_open(path, flags, *args)
        if Path(path) == lock:
            os.replace(replacement, lock)
        return fd

    def record_fstat(fd):
        fstat_calls.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(slot_module.os, "open", open_and_swap)
    monkeypatch.setattr(slot_module.os, "fstat", record_fstat)
    fd = slot_module._lock_open(lock, os.O_RDWR)
    try:
        assert real_fstat(fd).st_ino == original_inode
        assert fstat_calls == [fd]
    finally:
        os.close(fd)


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root for operation-lock ownership checks")
def test_operation_lock_requires_canonical_gameslot_inode(tmp_path, monkeypatch):
    try:
        gameslot_gid = grp.getgrnam("gameslot").gr_gid
    except KeyError:
        pytest.skip("gameslot group unavailable")
    lock = tmp_path / "operation.lock"
    lock.touch()
    os.chown(lock, 0, gameslot_gid)
    os.chmod(lock, 0o640)
    monkeypatch.setattr(slot_module, "OPERATION_LOCK", lock)
    with pytest.raises(ValueError):
        slot_module._lock_open(lock, os.O_RDWR)
    os.chmod(lock, 0o660)
    fd = slot_module._lock_open(lock, os.O_RDWR)
    os.close(fd)


@pytest.fixture
def slot_env(tmp_path: Path):
    operation = tmp_path / "operation.lock"
    slot = tmp_path / "slot.lock"
    metadata = tmp_path / "metadata.json"
    reservation = tmp_path / "reservation.json"
    operation.touch()
    slot.touch()
    return type(
        "SlotEnv",
        (),
        {
            "operation_path": operation,
            "slot_path": slot,
            "metadata_path": metadata,
            "reservation_path": reservation,
            "inspector": SlotInspector(slot, metadata),
            "store": ReservationStore(operation, reservation),
        },
    )()


def test_stale_metadata_never_blocks_when_flock_is_free(slot_env):
    slot_env.metadata_path.write_text(
        json.dumps({"pid": 999999, "profile_id": "minecraft", "proc_start_ticks": 1})
    )
    observed = slot_env.inspector.observe()
    assert observed.owner is None
    assert not slot_env.metadata_path.exists()


def test_wrong_profile_live_reservation_blocks_runner(slot_env, monkeypatch):
    slot_env.store.reserve("minecraft", "op-1", ttl=30)
    monkeypatch.setattr(os, "getpid", lambda: 12345)
    assert slot_env.store.valid_for_runner("pz-rising") is False


def test_matching_reservation_can_acquire_and_expiry_is_ignored(slot_env):
    slot_env.store.reserve("minecraft", "op-1", ttl=30)
    assert slot_env.store.valid_for_runner("minecraft") is True
    slot_env.reservation_path.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op-1",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": 1,
                "expires_at": 0,
            }
        )
    )
    assert slot_env.store.valid_for_runner("minecraft") is None


def test_unrepresentable_expiry_is_treated_as_stale(slot_env):
    slot_env.reservation_path.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op-1",
                "state_generation": 1,
                "controller_pid": os.getpid(),
                "controller_start_ticks": slot_env.store.pid_start_ticks(os.getpid()),
                "expires_at": 10**1000,
            }
        )
    )
    assert slot_env.store.read() is None
    assert slot_env.store.valid_for_runner("minecraft") is None


def test_reservation_expiry_is_computed_after_exclusive_lock(slot_env, monkeypatch):
    now = [100.0]
    store = ReservationStore(
        slot_env.operation_path,
        slot_env.reservation_path,
        clock=lambda: now[0],
    )
    holder = os.open(slot_env.operation_path, os.O_RDWR)
    import fcntl

    fcntl.flock(holder, fcntl.LOCK_EX)
    transaction_started = threading.Event()
    original_transaction = slot_module.operation_transaction

    def tracked_transaction(path):
        transaction_started.set()
        return original_transaction(path)

    monkeypatch.setattr(slot_module, "operation_transaction", tracked_transaction)
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(store.reserve("minecraft", "op-fresh", ttl=10))
    )
    thread.start()
    assert transaction_started.wait(timeout=2)
    now[0] = 200.0
    fcntl.flock(holder, fcntl.LOCK_UN)
    os.close(holder)
    thread.join(timeout=2)
    assert result
    assert result[0].expires_at == 210.0


def test_dead_controller_reservation_is_reconciled(slot_env):
    slot_env.reservation_path.write_text(
        json.dumps(
            {
                "profile_id": "minecraft",
                "operation_id": "op-1",
                "state_generation": 1,
                "controller_pid": 999999,
                "controller_start_ticks": 1,
                "expires_at": 4_000_000_000,
            }
        )
    )
    assert slot_env.store.valid_for_runner("pz-rising") is None
    assert slot_env.store.reconcile() is True
    assert not slot_env.reservation_path.exists()


def test_reservation_renewal_requires_matching_owner(slot_env):
    reservation = slot_env.store.reserve("minecraft", "switch-1", ttl=10, state_generation=2)
    renewed = slot_env.store.renew_if_owned("minecraft", "switch-1", ttl=20, state_generation=2)
    assert renewed.expires_at > reservation.expires_at
    with pytest.raises(BlockingIOError):
        slot_env.store.renew_if_owned("pz-rising", "switch-1", ttl=20, state_generation=2)


def test_same_profile_different_operation_cannot_replace_live_lease(slot_env):
    slot_env.store.reserve("minecraft", "op-1", ttl=10)
    with pytest.raises(BlockingIOError):
        slot_env.store.reserve_if_available("minecraft", "op-2", ttl=10)
    assert slot_env.store.read().operation_id == "op-1"


def test_release_if_owned_does_not_delete_replacement(slot_env):
    slot_env.store.reserve("minecraft", "op-1", ttl=10, state_generation=1)
    assert slot_env.store.release_if_owned("minecraft", "op-2", 1) is False
    assert slot_env.store.read() is not None
    assert slot_env.store.release_if_owned("minecraft", "op-1", 1) is True
    assert slot_env.store.read() is None


def test_transfer_if_owned_keeps_lease_continuity(slot_env):
    slot_env.store.reserve("minecraft", "op-1", ttl=10, state_generation=1)
    moved = slot_env.store.transfer_if_owned("minecraft", "op-1", 1, "pz-rising", "rollback-1")
    assert moved.profile_id is ProfileId.PZ_RISING
    assert slot_env.store.read().operation_id == "rollback-1"


def test_reconcile_rechecks_atomically_replaced_reservation(slot_env, monkeypatch):
    slot_env.reservation_path.write_text("{not-json")
    done: list[bool] = []
    writer_done = threading.Event()
    writer_error: list[BaseException] = []
    original_read = slot_module._read_json
    first_read = True

    def read_with_writer(path: Path):
        nonlocal first_read
        raw = original_read(path)
        if path == slot_env.reservation_path and first_read:
            first_read = False

            def writer() -> None:
                try:
                    slot_env.store.reserve("minecraft", "op-live", ttl=10, state_generation=1)
                except BaseException as exc:
                    writer_error.append(exc)
                finally:
                    writer_done.set()

            threading.Thread(target=writer).start()
            assert writer_done.wait(timeout=2)
        return raw

    monkeypatch.setattr(slot_module, "_read_json", read_with_writer)
    thread = threading.Thread(target=lambda: done.append(slot_env.store.reconcile()))
    thread.start()
    thread.join(timeout=2)
    assert done == [False]
    assert slot_env.reservation_path.exists()
    assert not writer_error
    mode = stat.S_IMODE(slot_env.reservation_path.stat().st_mode)
    assert (slot_env.reservation_path.stat().st_uid, slot_env.reservation_path.stat().st_gid, mode) == (0, 0, 0o644)
