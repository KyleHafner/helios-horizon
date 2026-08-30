"""Bounded, restart-safe following of a regular game log.

The follower deliberately persists only the file identity and the offset of the
last successfully delivered complete line.  In particular, a checkpoint never
contains log text (or player names).  This makes a partial final line safe to
re-read after a process restart and keeps the checkpoint useful for recovery.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from .redaction import Redactor

_SCHEMA_VERSION = 1


class LogFollowerError(RuntimeError):
    """The follower could not safely consume the configured path."""


class UnsafeLogPathError(LogFollowerError):
    """The path was a symlink, non-regular file, or changed during a read."""


class LineTooLargeError(LogFollowerError):
    """A line exceeded the configured bounded line size."""


@dataclass(frozen=True)
class LogEvent:
    kind: Literal["line", "reset"]
    epoch: tuple[int, int]
    offset: int
    line: str | None = None
    reason: str | None = None


Callback = Callable[[LogEvent], Any | Awaitable[Any]]


class LogFollower:
    """Follow one safe regular file, delivering each complete line once.

    ``checkpoint`` is optional; when supplied, it is atomically replaced after
    every successful callback.  A callback exception leaves the failed line
    undelivered in the checkpoint and is re-tried by the next call.
    """

    def __init__(
        self,
        path: str | Path,
        checkpoint: str | Path | None = None,
        *,
        max_line_bytes: int = 64 * 1024,
        max_read_bytes: int = 256 * 1024,
        max_file_bytes: int = 1 * 1024 * 1024 * 1024,
        sanitizer: Callable[[str], str] | None = None,
        start_at_end: bool = False,
    ) -> None:
        self.path = Path(path)
        self.checkpoint = Path(checkpoint) if checkpoint is not None else None
        if not 1 <= max_line_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_line_bytes out of bounds")
        if not 1 <= max_read_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_read_bytes out of bounds")
        if max_file_bytes < 1 or max_file_bytes > 64 * 1024 * 1024 * 1024:
            raise ValueError("max_file_bytes out of bounds")
        self.max_line_bytes = max_line_bytes
        self.max_read_bytes = max_read_bytes
        self.max_file_bytes = max_file_bytes
        self._sanitize = sanitizer or Redactor().redact
        self._partial = b""
        self._epoch: tuple[int, int] | None = None
        self._offset = 0
        self._read_offset = 0
        self._loaded = False
        self._start_at_end = bool(start_at_end)

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.checkpoint is None:
            return
        self._validate_checkpoint_path(require_file=False)
        if not self.checkpoint.exists():
            return
        fd = self._open_checkpoint_read()
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                value = json.load(stream)
            if value.get("schema_version") != _SCHEMA_VERSION:
                raise LogFollowerError("unsupported log follower checkpoint schema")
            device, inode = value["device"], value["inode"]
            offset = value["offset"]
            if (
                not isinstance(device, int)
                or not isinstance(inode, int)
                or not isinstance(offset, int)
                or device < 0
                or inode < 0
                or offset < 0
            ):
                raise ValueError
            self._epoch = (device, inode)
            self._offset = offset
            self._read_offset = offset
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise LogFollowerError("invalid log follower checkpoint") from exc

    def _open_checkpoint_read(self) -> int:
        if self.checkpoint is None:
            raise LogFollowerError("checkpoint is unavailable")
        self._validate_checkpoint_path(require_file=True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.checkpoint, flags)
            opened = os.fstat(fd)
            current = self.checkpoint.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or stat.S_IMODE(opened.st_mode) != 0o600:
                os.close(fd)
                raise LogFollowerError("checkpoint changed during open")
            return fd
        except OSError as exc:
            raise LogFollowerError("checkpoint could not be opened safely") from exc

    def _validate_checkpoint_path(self, *, require_file: bool) -> None:
        if self.checkpoint is None:
            return
        try:
            parent_info = self.checkpoint.parent.lstat()
        except OSError as exc:
            raise LogFollowerError("checkpoint parent is unavailable") from exc
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise LogFollowerError("checkpoint parent must be a directory")
        if parent_info.st_uid != os.geteuid() or parent_info.st_gid != os.getegid():
            raise LogFollowerError("checkpoint parent has unexpected ownership")
        if parent_info.st_mode & 0o077:
            raise LogFollowerError("checkpoint parent is not private")
        try:
            info = self.checkpoint.lstat()
        except FileNotFoundError:
            if require_file:
                raise LogFollowerError("checkpoint is unavailable")
            return
        except OSError as exc:
            raise LogFollowerError("checkpoint is unavailable") from exc
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
            raise LogFollowerError("checkpoint must be a regular 0600 file")

    def _save(self) -> None:
        if self.checkpoint is None or self._epoch is None:
            return
        self._validate_checkpoint_path(require_file=False)
        parent_fd = os.open(self.checkpoint.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        parent_identity = os.fstat(parent_fd)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "device": self._epoch[0],
            "inode": self._epoch[1],
            "offset": self._offset,
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.checkpoint.name}.", dir=self.checkpoint.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            current_parent = os.fstat(parent_fd)
            current_path_parent = self.checkpoint.parent.lstat()
            if (current_parent.st_dev, current_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino) or (current_path_parent.st_dev, current_path_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
                raise LogFollowerError("checkpoint parent changed during save")
            os.replace(temporary, self.checkpoint)
            os.fsync(parent_fd)
            self._validate_checkpoint_path(require_file=True)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            os.close(parent_fd)
            raise
        else:
            os.close(parent_fd)

    def _open_safe(self) -> tuple[int, tuple[int, int], int]:
        try:
            before = self.path.lstat()
        except OSError as exc:
            raise UnsafeLogPathError("log path unavailable") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise UnsafeLogPathError("log path is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise UnsafeLogPathError("log path could not be opened safely") from exc
        try:
            opened = os.fstat(fd)
            after = self.path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(after.st_mode)
                or not stat.S_ISREG(after.st_mode)
                or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise UnsafeLogPathError("log path changed during open")
            return fd, (opened.st_dev, opened.st_ino), opened.st_size
        except Exception:
            os.close(fd)
            raise

    @staticmethod
    async def _deliver(callback: Callback | None, event: LogEvent) -> None:
        if callback is None:
            return
        result = callback(event)
        if inspect.isawaitable(result):
            await result

    def follow(self, callback: Callback | None = None) -> tuple[LogEvent, ...]:
        """Synchronously consume one bounded chunk and invoke ``callback``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.follow_async(callback))
        raise RuntimeError("follow() cannot run inside an event loop; use follow_async()")

    async def follow_async(self, callback: Callback | None = None) -> tuple[LogEvent, ...]:
        self._load()
        fd, epoch, size = self._open_safe()
        events: list[LogEvent] = []
        try:
            if size > self.max_file_bytes:
                raise LogFollowerError("log file exceeds configured limit")
            reset_reason: str | None = None
            if self._epoch is None:
                self._epoch = epoch
                if self._start_at_end:
                    self._offset = self._read_offset = size
                    self._partial = b""
                    self._save()
                    return tuple(events)
                self._offset, self._read_offset, self._partial = 0, 0, b""
            elif self._epoch != epoch:
                reset_reason = "rotation"
            elif size < max(self._offset, self._read_offset):
                reset_reason = "truncation"
            if reset_reason is not None:
                marker = LogEvent("reset", epoch, 0, reason=reset_reason)
                await self._deliver(callback, marker)
                events.append(marker)
                self._epoch, self._offset, self._read_offset, self._partial = epoch, 0, 0, b""
                self._save()
            if self._offset > size:
                raise LogFollowerError("checkpoint offset exceeds file size")
            data = os.pread(fd, self.max_read_bytes, self._read_offset)
            if not data:
                return tuple(events)
            read_end = self._read_offset + len(data)
            check = self.path.lstat()
            final = os.fstat(fd)
            if (final.st_dev, final.st_ino) != epoch or (check.st_dev, check.st_ino) != epoch:
                raise UnsafeLogPathError("log path changed during read")
            buffer = self._partial + data
            cursor = 0
            origin = self._offset
            while True:
                newline = buffer.find(b"\n", cursor)
                if newline < 0:
                    remaining = buffer[cursor:]
                    if len(remaining) > self.max_line_bytes:
                        raise LineTooLargeError("log line exceeds configured limit")
                    self._partial = remaining
                    self._read_offset = read_end
                    break
                raw = buffer[cursor:newline]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if len(raw) > self.max_line_bytes:
                    raise LineTooLargeError("log line exceeds configured limit")
                try:
                    text = raw.decode("utf-8", "replace")
                    sanitized = self._sanitize(text)
                    if not isinstance(sanitized, str) or len(sanitized) > self.max_line_bytes:
                        raise LineTooLargeError("sanitized log line exceeds configured limit")
                except Exception as exc:
                    if isinstance(exc, LineTooLargeError):
                        raise
                    raise LogFollowerError("log line sanitization failed") from exc
                # ``newline`` is relative to the complete buffer, whose
                # origin is the last durable offset. Do not add prior line
                # ends again when delivering multiple lines from one read.
                end = origin + newline + 1
                event = LogEvent("line", epoch, end, line=sanitized)
                await self._deliver(callback, event)
                events.append(event)
                self._offset, self._partial = end, b""
                self._save()
                cursor = newline + 1
            return tuple(events)
        except Exception:
            # Never let an in-memory partial or read cursor skip a line after
            # a failed callback or malformed record.  Successful lines have
            # already advanced the durable offset one at a time.
            self._partial = b""
            self._read_offset = self._offset
            raise
        finally:
            os.close(fd)


__all__ = [
    "LineTooLargeError",
    "LogEvent",
    "LogFollower",
    "LogFollowerError",
    "UnsafeLogPathError",
]
