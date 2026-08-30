from __future__ import annotations

import errno
import fcntl
import grp
import json
import math
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .models import ProfileId


OPERATION_LOCK = Path("/run/game-control/operation.lock")
SLOT_LOCK = Path("/run/game-control/slot.lock")
SLOT_METADATA = Path("/run/game-slot/slot.json")
RESERVATION_FILE = Path("/run/game-control/reservation.json")
MAX_RESERVATION_TTL = 30.0


def proc_start_ticks(pid: int) -> int | None:
    """Return Linux's process start time (field 22 of /proc/<pid>/stat)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        _, fields = text.split(") ", 1)
        # fields starts at stat field 3, so field 22 is offset 19.
        return int(fields.split()[19])
    except (ValueError, IndexError):
        return None


def _profile(value: str | ProfileId) -> ProfileId:
    try:
        return value if isinstance(value, ProfileId) else ProfileId(value)
    except ValueError as exc:
        raise ValueError("invalid profile id") from exc


@dataclass(frozen=True)
class SlotObservation:
    owner: str | None
    pid: int | None = None
    proc_start_ticks: int | None = None
    inconsistent: bool = False


@dataclass(frozen=True)
class Reservation:
    profile_id: ProfileId
    operation_id: str
    state_generation: int
    controller_pid: int
    controller_start_ticks: int
    expires_at: float


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _reservation_from_json(raw: dict[str, object] | None) -> Reservation | None:
    if raw is None:
        return None
    try:
        profile = _profile(raw["profile_id"])
        operation_id = raw["operation_id"]
        generation = raw["state_generation"]
        pid = raw["controller_pid"]
        ticks = raw["controller_start_ticks"]
        expires = raw["expires_at"]
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(ticks, int)
            or isinstance(ticks, bool)
            or ticks < 0
            or not isinstance(expires, (int, float))
            or isinstance(expires, bool)
        ):
            return None
        try:
            expiry = float(expires)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(expiry):
            return None
        return Reservation(profile, operation_id, generation, pid, ticks, expiry)
    except (KeyError, TypeError, ValueError):
        return None


def _lock_open(path: Path, flags: int) -> int:
    # Lock files are installed by tmpfiles; never create or replace one here.
    fd = os.open(path, flags | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if Path(path) in (SLOT_LOCK, OPERATION_LOCK):
            try:
                gameslot_gid = grp.getgrnam("gameslot").gr_gid
            except KeyError as exc:
                raise ValueError("gameslot group is unavailable") from exc
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != gameslot_gid
                or mode != 0o660
                or info.st_nlink != 1
            ):
                raise ValueError("slot lock is not root:gameslot 0660")
        elif (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or mode & 0o002
            or mode & 0o600 != 0o600
        ):
            raise ValueError("lock is not root-owned and secure")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def operation_transaction(path: Path = OPERATION_LOCK) -> Iterator[int]:
    fd = _lock_open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class OperationLock:
    """A bounded-scope operation lock transaction.

    The slot runner uses a shared transaction directly; root transitions use
    the exclusive mode exposed here.  Neither mode ever acquires the slot.
    """

    def __init__(self, path: Path = OPERATION_LOCK, *, shared: bool = False):
        self.path = Path(path)
        self.shared = shared
        self.fd: int | None = None

    def __enter__(self) -> int:
        self.fd = _lock_open(self.path, os.O_RDWR)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX)
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self.fd

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _atomic_json(path: Path, payload: dict[str, object], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        try:
            os.fchown(fd, 0, 0)
        except PermissionError:
            pass
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


class ReservationStore:
    def __init__(
        self,
        operation_path: Path = OPERATION_LOCK,
        reservation_path: Path = RESERVATION_FILE,
        *,
        clock=time.time,
        pid_start_ticks=proc_start_ticks,
    ):
        self.operation_path = Path(operation_path)
        self.reservation_path = Path(reservation_path)
        self.clock = clock
        self.pid_start_ticks = pid_start_ticks

    def read(self) -> Reservation | None:
        return _reservation_from_json(_read_json(self.reservation_path))

    def reserve(
        self,
        profile: str | ProfileId,
        operation_id: str,
        ttl: float,
        *,
        state_generation: int = 0,
        controller_pid: int | None = None,
        controller_start_ticks: int | None = None,
    ) -> Reservation:
        if os.geteuid() != 0:
            raise PermissionError("only root may create reservations")
        profile_id = _profile(profile)
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("invalid operation id")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(float(ttl)):
            raise ValueError("invalid reservation ttl")
        if not 0 < ttl <= MAX_RESERVATION_TTL:
            raise ValueError("reservation ttl must be at most 30 seconds")
        if not isinstance(state_generation, int) or state_generation < 0:
            raise ValueError("invalid state generation")
        pid = os.getpid() if controller_pid is None else controller_pid
        ticks = self.pid_start_ticks(pid) if controller_start_ticks is None else controller_start_ticks
        if ticks is None:
            raise ValueError("controller process does not exist")
        with operation_transaction(self.operation_path):
            reservation = Reservation(
                profile_id,
                operation_id,
                state_generation,
                pid,
                ticks,
                self.clock() + ttl,
            )
            _atomic_json(self.reservation_path, {
                "profile_id": reservation.profile_id.value,
                "operation_id": reservation.operation_id,
                "state_generation": reservation.state_generation,
                "controller_pid": reservation.controller_pid,
                "controller_start_ticks": reservation.controller_start_ticks,
                "expires_at": reservation.expires_at,
            })
        return reservation

    def reserve_if_available(
        self,
        profile: str | ProfileId,
        operation_id: str,
        ttl: float,
        *,
        state_generation: int = 0,
        controller_pid: int | None = None,
        controller_start_ticks: int | None = None,
    ) -> Reservation:
        """Atomically check the live owner and commit a reservation.

        Controllers must not perform a read followed by ``reserve``: another
        writer can win the gap between those calls.  This method keeps both
        operations inside the same exclusive operation-lock transaction.
        """
        if os.geteuid() != 0:
            raise PermissionError("only root may create reservations")
        profile_id = _profile(profile)
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("invalid operation id")
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not math.isfinite(float(ttl)):
            raise ValueError("invalid reservation ttl")
        if not 0 < ttl <= MAX_RESERVATION_TTL:
            raise ValueError("reservation ttl must be at most 30 seconds")
        if not isinstance(state_generation, int) or state_generation < 0:
            raise ValueError("invalid state generation")
        pid = os.getpid() if controller_pid is None else controller_pid
        ticks = self.pid_start_ticks(pid) if controller_start_ticks is None else controller_start_ticks
        if ticks is None:
            raise ValueError("controller process does not exist")
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            if (
                current is not None
                and self._live(current)
                and (current.profile_id != profile_id or current.operation_id != operation_id)
            ):
                raise BlockingIOError("reservation belongs to another profile")
            reservation = Reservation(
                profile_id, operation_id, state_generation, pid, ticks,
                self.clock() + ttl,
            )
            _atomic_json(self.reservation_path, {
                "profile_id": reservation.profile_id.value,
                "operation_id": reservation.operation_id,
                "state_generation": reservation.state_generation,
                "controller_pid": reservation.controller_pid,
                "controller_start_ticks": reservation.controller_start_ticks,
                "expires_at": reservation.expires_at,
            })
        return reservation

    def release_if_owned(
        self,
        profile: str | ProfileId,
        operation_id: str,
        state_generation: int = 0,
    ) -> bool:
        profile_id = _profile(profile)
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            if (
                current is None
                or current.profile_id != profile_id
                or current.operation_id != operation_id
                or current.state_generation != state_generation
            ):
                return False
            self.reservation_path.unlink(missing_ok=True)
            return True

    def owns_live(
        self,
        profile: str | ProfileId,
        operation_id: str,
        state_generation: int = 0,
    ) -> bool:
        """Atomically verify exact ownership, expiry, and controller liveness."""
        profile_id = _profile(profile)
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            return bool(
                current is not None
                and current.profile_id == profile_id
                and current.operation_id == operation_id
                and current.state_generation == state_generation
                and self._live(current)
            )

    def transfer_if_owned(
        self,
        profile: str | ProfileId,
        operation_id: str,
        state_generation: int,
        target_profile: str | ProfileId,
        target_operation_id: str,
        ttl: float = 30.0,
    ) -> Reservation:
        """Atomically hand a lease to rollback/source ownership."""
        source = _profile(profile)
        target = _profile(target_profile)
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            if (
                current is None
                or current.profile_id != source
                or current.operation_id != operation_id
                or current.state_generation != state_generation
            ):
                raise BlockingIOError("reservation ownership changed")
            renewed = Reservation(
                target, target_operation_id, state_generation,
                current.controller_pid, current.controller_start_ticks,
                self.clock() + ttl,
            )
            _atomic_json(self.reservation_path, {
                "profile_id": renewed.profile_id.value,
                "operation_id": renewed.operation_id,
                "state_generation": renewed.state_generation,
                "controller_pid": renewed.controller_pid,
                "controller_start_ticks": renewed.controller_start_ticks,
                "expires_at": renewed.expires_at,
            })
            return renewed

    def renew_if_owned(
        self,
        profile: str | ProfileId,
        operation_id: str,
        ttl: float,
        *,
        state_generation: int = 0,
    ) -> Reservation:
        """Extend only the lease this operation currently owns."""
        profile_id = _profile(profile)
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            if (
                current is None
                or current.profile_id != profile_id
                or current.operation_id != operation_id
                or current.state_generation != state_generation
                or not self._live(current)
            ):
                raise BlockingIOError("reservation ownership changed")
            renewed = Reservation(
                current.profile_id, current.operation_id, current.state_generation,
                current.controller_pid, current.controller_start_ticks,
                self.clock() + ttl,
            )
            _atomic_json(self.reservation_path, {
                "profile_id": renewed.profile_id.value,
                "operation_id": renewed.operation_id,
                "state_generation": renewed.state_generation,
                "controller_pid": renewed.controller_pid,
                "controller_start_ticks": renewed.controller_start_ticks,
                "expires_at": renewed.expires_at,
            })
            return renewed

    def valid_for_runner(self, profile: str | ProfileId) -> bool | None:
        """Return True for a matching reservation, False for a live mismatch.

        None means absent or stale.  Runners deliberately do not rewrite this
        root-owned file; slotd's reconciliation removes stale records.
        """
        requested = _profile(profile)
        reservation = self.read()
        if reservation is None or not self._live(reservation):
            return None
        return reservation.profile_id == requested

    validate_for_runner = valid_for_runner

    def reconcile(self) -> bool:
        """Remove an invalid, expired, or dead-controller reservation."""
        if os.geteuid() != 0:
            raise PermissionError("only root may reconcile reservations")
        reservation = self.read()
        if reservation is not None and self._live(reservation):
            return False
        with operation_transaction(self.operation_path):
            current = _reservation_from_json(_read_json(self.reservation_path))
            if current is None or not self._live(current):
                if self.reservation_path.exists():
                    self.reservation_path.unlink(missing_ok=True)
                    return True
        return False

    def _live(self, reservation: Reservation) -> bool:
        now = self.clock()
        return (
            now < reservation.expires_at <= now + MAX_RESERVATION_TTL
            and self.pid_start_ticks(reservation.controller_pid) == reservation.controller_start_ticks
        )

    renew = reserve


class SlotInspector:
    def __init__(
        self,
        slot_path: Path = SLOT_LOCK,
        metadata_path: Path = SLOT_METADATA,
        *,
        pid_start_ticks=proc_start_ticks,
    ):
        self.slot_path = Path(slot_path)
        self.metadata_path = Path(metadata_path)
        self.pid_start_ticks = pid_start_ticks

    def _clear_metadata(self) -> None:
        try:
            self.metadata_path.unlink()
        except FileNotFoundError:
            pass

    def observe(self) -> SlotObservation:
        fd = _lock_open(self.slot_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._observe_held_slot()
            self._clear_metadata()
            return SlotObservation(owner=None)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _observe_held_slot(self) -> SlotObservation:
        raw = _read_json(self.metadata_path)
        if raw is None:
            return SlotObservation(owner=None, inconsistent=True)
        try:
            owner = _profile(raw.get("profile_id", raw.get("profile")))
            pid = raw["pid"]
            ticks = raw["proc_start_ticks"]
            if not isinstance(pid, int) or pid <= 0 or not isinstance(ticks, int) or ticks < 0:
                raise ValueError
            if self.pid_start_ticks(pid) != ticks:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return SlotObservation(owner=None, inconsistent=True)
        return SlotObservation(owner=owner.value, pid=pid, proc_start_ticks=ticks)
