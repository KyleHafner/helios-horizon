"""Bounded authenticated watch primitives for Horizon's event-driven path.

This module is deliberately transport-neutral.  ``WatchHub`` is the typed
broker used by the Unix watch adapter and the web SSE projection; it owns
sequence/generation ordering, replay cursors, slow-client isolation, and
full-resync signalling.  It never accepts paths, commands, URLs, or
credentials in event payloads.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Mapping


class WatchProtocolError(ValueError):
    """The producer attempted to violate the monotonic watch contract."""


@dataclass(frozen=True, slots=True)
class WatchEvent:
    sequence: int
    generation: int
    kind: str
    payload: Mapping[str, Any]
    full: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.generation < 0:
            raise WatchProtocolError("watch sequence and generation must be non-negative")
        if not self.kind or len(self.kind) > 64 or any(ch.isspace() for ch in self.kind):
            raise WatchProtocolError("invalid watch event kind")


@dataclass(frozen=True, slots=True)
class WatchCursor:
    sequence: int = 0
    generation: int = 0

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.generation < 0:
            raise WatchProtocolError("watch cursor must be non-negative")


@dataclass(slots=True)
class WatchClient:
    queue: asyncio.Queue[WatchEvent]
    cursor: WatchCursor
    overflow_count: int = 0
    disconnected: bool = False
    resync_required: bool = False


@dataclass(frozen=True, slots=True)
class LockTiming:
    read_count: int
    write_count: int
    read_wait_ms: float
    write_wait_ms: float


class ReadWriteLock:
    """Small asyncio RW lock with bounded timing counters.

    Readers may run concurrently; writers exclude both readers and writers.
    Timing is observational only and never part of the synchronization path.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._read_count = self._write_count = 0
        self._read_wait = self._write_wait = 0.0

    async def read(self):
        started = time.monotonic()
        async with self._condition:
            while self._writer or self._waiting_writers:
                await self._condition.wait()
            self._readers += 1
            self._read_count += 1
            self._read_wait += (time.monotonic() - started) * 1000.0
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if not self._readers:
                    self._condition.notify_all()

    async def write(self):
        started = time.monotonic()
        async with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    await self._condition.wait()
                self._writer = True
                self._write_count += 1
                self._write_wait += (time.monotonic() - started) * 1000.0
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer = False
                self._condition.notify_all()

    def timing(self) -> LockTiming:
        return LockTiming(self._read_count, self._write_count, self._read_wait, self._write_wait)


# Keep the lock's hot path explicit while retaining ergonomic ``async with``
# usage for callers.
ReadWriteLock.read = asynccontextmanager(ReadWriteLock.read)
ReadWriteLock.write = asynccontextmanager(ReadWriteLock.write)


class WatchHub:
    """Bounded event broker with replay, resync, and slow-client isolation."""

    def __init__(self, *, queue_size: int = 32, history_size: int = 256, max_overflows: int = 3):
        if not 1 <= queue_size <= 1024 or not 1 <= history_size <= 4096:
            raise ValueError("watch bounds out of range")
        if not 1 <= max_overflows <= 32:
            raise ValueError("overflow bound out of range")
        self.queue_size = queue_size
        self.history_size = history_size
        self.max_overflows = max_overflows
        self._history: deque[WatchEvent] = deque(maxlen=history_size)
        self._clients: set[int] = set()
        self._client_map: dict[int, WatchClient] = {}
        self._next_client = 0
        self._sequence = 0
        self._generation = 0
        self._snapshot: WatchEvent | None = None
        self._lock = ReadWriteLock()

    @property
    def generation(self) -> int:
        return self._generation

    def timing(self) -> LockTiming:
        return self._lock.timing()

    async def subscribe(self, cursor: WatchCursor = WatchCursor()) -> WatchClient:
        client = WatchClient(asyncio.Queue(maxsize=self.queue_size), cursor)
        async with self._lock.write():
            self._next_client += 1
            ident = self._next_client
            self._clients.add(ident)
            self._client_map[ident] = client
            replay = [item for item in self._history if item.sequence > cursor.sequence]
            if replay and replay[0].sequence > cursor.sequence + 1:
                self._enqueue_resync(client)
            elif cursor.generation > self._generation:
                self._enqueue_resync(client)
            elif replay:
                # A full snapshot supersedes all earlier deltas.  Replay it
                # first, followed only by later deltas, bounded to the queue.
                if self._snapshot is not None and self._snapshot.sequence > cursor.sequence:
                    replay = [self._snapshot] + [item for item in replay if item.sequence > self._snapshot.sequence]
                for item in replay[-self.queue_size:]:
                    self._enqueue(client, item)
            elif self._snapshot is not None and self._snapshot.generation > cursor.generation:
                self._enqueue(client, self._snapshot)
        return client

    async def unsubscribe(self, client: WatchClient) -> None:
        async with self._lock.write():
            client.disconnected = True
            for ident, candidate in tuple(self._client_map.items()):
                if candidate is client:
                    self._clients.discard(ident)
                    self._client_map.pop(ident, None)

    async def publish(self, kind: str, payload: Mapping[str, Any], *, generation: int,
                      full: bool = False) -> WatchEvent:
        if generation < self._generation:
            raise WatchProtocolError("stale generation")
        async with self._lock.write():
            if generation < self._generation:
                raise WatchProtocolError("stale generation")
            self._sequence += 1
            self._generation = generation
            item = WatchEvent(self._sequence, generation, kind, dict(payload), full)
            self._history.append(item)
            if full:
                self._snapshot = item
            for ident in tuple(self._clients):
                client = self._client_map.get(ident)
                if client is None or client.disconnected:
                    self._clients.discard(ident)
                    continue
                self._enqueue(client, item)
                if client.disconnected:
                    self._clients.discard(ident)
                    self._client_map.pop(ident, None)
            return item

    def _enqueue(self, client: WatchClient, item: WatchEvent) -> None:
        if client.disconnected:
            return
        try:
            client.queue.put_nowait(item)
            client.cursor = WatchCursor(item.sequence, item.generation)
            return
        except asyncio.QueueFull:
            client.overflow_count += 1
            client.resync_required = True
        # Replace stale queued deltas with one explicit full-resync marker.
        while not client.queue.empty():
            try:
                client.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # Reserve a distinct broker sequence for the marker.  The dropped
        # event remains in history, while the cursor advances past both it
        # and the resync request; the next real event can never reuse either
        # identity.
        self._sequence += 1
        marker = WatchEvent(self._sequence, item.generation, "full_resync", {}, full=True)
        try:
            client.queue.put_nowait(marker)
        except asyncio.QueueFull:
            client.disconnected = True
            return
        client.cursor = WatchCursor(item.sequence, item.generation)
        if client.overflow_count >= self.max_overflows:
            client.disconnected = True

    def _enqueue_resync(self, client: WatchClient) -> None:
        if self._snapshot is not None:
            self._enqueue(client, self._snapshot)
        else:
            self._sequence += 1
            marker = WatchEvent(self._sequence, self._generation, "full_resync", {}, full=True)
            self._enqueue(client, marker)

    async def heartbeat(self, client: WatchClient, *, timeout: float = 15.0) -> WatchEvent:
        if timeout <= 0 or timeout > 300:
            raise ValueError("heartbeat timeout out of range")
        try:
            return await asyncio.wait_for(client.queue.get(), timeout)
        except asyncio.TimeoutError:
            return WatchEvent(self._sequence, self._generation, "heartbeat", {}, full=False)


__all__ = ["LockTiming", "ReadWriteLock", "WatchClient", "WatchCursor", "WatchEvent", "WatchHub", "WatchProtocolError"]
