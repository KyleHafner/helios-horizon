"""Owned, read-only history and statistics queries.

The controller's state database is a writer owned by the root assembly.  This
module deliberately never borrows that writer connection for file-backed
queries: every operation opens its own short-lived read-only connection in the
bounded per-service executor.  A raw sqlite connection remains a small,
explicit in-memory test seam because sqlite connections are thread-affine.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import inspect
import json
import sqlite3
import threading
from concurrent.futures import Executor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import SafeError
from .models import ProfileId
from .protocol import (
    AuditPage,
    AuditSummary,
    ErrorCode,
    EventPage,
    EventSummary,
)
from .state_db import STATE_DB_PATH
from .stats_queries import (
    stats_heatmap as query_stats_heatmap,
    stats_summary as query_stats_summary,
    stats_tps as query_stats_tps,
)


_UNAVAILABLE = "operational state is unavailable"


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _error_code(value: Any) -> ErrorCode | None:
    try:
        return ErrorCode(value) if value else None
    except ValueError:
        return None


class HistoryQueryService:
    """The single production owner for audit, events, and public stats."""

    def __init__(
        self,
        state_database: Any,
        telemetry_database: Any | None = None,
        *,
        approved_state_path: Path = STATE_DB_PATH,
        approved_telemetry_path: Path | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.state_database = state_database
        self.telemetry_database = telemetry_database
        self.approved_state_path = Path(approved_state_path)
        self.approved_telemetry_path = (
            None if approved_telemetry_path is None else Path(approved_telemetry_path)
        )
        self._executor = executor if executor is not None else concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="horizon-history-query"
        )
        self._owns_executor = executor is None
        self._futures: set[Future[Any]] = set()
        self._future_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closing = False
        self._executor_closed = False
        self._close_task: asyncio.Task[None] | None = None

    @staticmethod
    def _safe_error(exc: BaseException) -> SafeError:
        if isinstance(exc, SafeError):
            return exc
        return SafeError("state_unavailable", _UNAVAILABLE)

    @staticmethod
    def _is_memory_database(database: Any) -> bool:
        return isinstance(database, sqlite3.Connection)

    @classmethod
    def _database_path(cls, database: Any, approved: Path, *, required: bool = True) -> Path:
        path = getattr(database, "path", None)
        if path is None:
            if required:
                raise SafeError("state_unavailable", _UNAVAILABLE)
            return approved
        try:
            candidate = Path(path)
            if candidate.absolute() != approved.absolute():
                raise SafeError("state_unavailable", _UNAVAILABLE)
        except (TypeError, ValueError, OSError) as exc:
            raise SafeError("state_unavailable", _UNAVAILABLE) from exc
        # Always open the approved spelling, even when a compatible relative
        # spelling was supplied by a test or an injected wrapper.
        return approved

    def _state_path(self) -> Path | None:
        if self._is_memory_database(self.state_database):
            return None
        return self._database_path(self.state_database, self.approved_state_path)

    def _telemetry_path(self) -> Path | None:
        database = self.telemetry_database
        if database is None or self._is_memory_database(database):
            return None
        approved = self.approved_telemetry_path
        if approved is None:
            approved = self.approved_state_path.with_name("telemetry.db")
        return self._database_path(database, approved)

    @staticmethod
    def _open_readonly(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=200")
        except BaseException:
            connection.close()
            raise
        return connection

    def _submit(self, operation: Callable[[], Any]) -> asyncio.Future[Any]:
        with self._state_lock:
            if self._closing:
                raise SafeError("state_unavailable", _UNAVAILABLE)
            future = self._executor.submit(operation)
            with self._future_lock:
                self._futures.add(future)
            future.add_done_callback(self._forget_future)
        return asyncio.wrap_future(future)

    def _forget_future(self, future: Future[Any]) -> None:
        try:
            future.exception()
        except BaseException:
            pass
        with self._future_lock:
            self._futures.discard(future)

    @staticmethod
    def _consume_async_future(future: asyncio.Future[Any]) -> None:
        try:
            future.exception()
        except BaseException:
            pass

    async def _run(self, operation: Callable[[], Any]) -> Any:
        try:
            wrapped = self._submit(operation)
            wrapped.add_done_callback(self._consume_async_future)
            # A task boundary keeps cancellation from turning the worker's
            # future into an unobserved late-exception carrier.  ``wait`` does
            # not propagate cancellation to the supplied task.
            waiter = asyncio.create_task(self._await_worker(wrapped))
            waiter.add_done_callback(self._consume_async_future)
            done, _pending = await asyncio.wait(
                (waiter,), return_when=asyncio.FIRST_COMPLETED
            )
            return next(iter(done)).result()
        except asyncio.CancelledError:
            # The concurrent future remains tracked until its worker finishes;
            # its operation closes all connections in its own finally block.
            raise
        except BaseException as exc:
            raise self._safe_error(exc) from exc

    @staticmethod
    async def _await_worker(future: asyncio.Future[Any]) -> Any:
        return await future

    def _query_memory(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except BaseException as exc:
            raise self._safe_error(exc) from exc

    def _with_state_connection(self, query: Callable[[sqlite3.Connection], Any]) -> Any:
        path = self._state_path()
        if path is None:
            database = self.state_database
            if not isinstance(database, sqlite3.Connection):
                raise SafeError("state_unavailable", _UNAVAILABLE)
            return self._query_memory(lambda: query(database))

        def operation() -> Any:
            connection: sqlite3.Connection | None = None
            try:
                connection = self._open_readonly(path)
                return query(connection)
            finally:
                if connection is not None:
                    connection.close()

        return self._run(operation)

    async def _with_state_connection_async(self, query: Callable[[sqlite3.Connection], Any]) -> Any:
        result = self._with_state_connection(query)
        return await result if inspect.isawaitable(result) else result

    def _with_tps_connections(self, query: Callable[[sqlite3.Connection, sqlite3.Connection | None], Any]) -> Any:
        state_path = self._state_path()
        telemetry_path = self._telemetry_path()
        if state_path is None:
            state = self.state_database
            if not isinstance(state, sqlite3.Connection):
                raise SafeError("state_unavailable", _UNAVAILABLE)
            telemetry = self.telemetry_database
            if telemetry is not None and not isinstance(telemetry, sqlite3.Connection):
                raise SafeError("state_unavailable", _UNAVAILABLE)
            return self._query_memory(lambda: query(state, telemetry))

        def operation() -> Any:
            state_connection: sqlite3.Connection | None = None
            telemetry_connection: sqlite3.Connection | None = None
            try:
                state_connection = self._open_readonly(state_path)
                if self.telemetry_database is not None:
                    if telemetry_path is None:
                        raise SafeError("state_unavailable", _UNAVAILABLE)
                    telemetry_connection = self._open_readonly(telemetry_path)
                return query(state_connection, telemetry_connection)
            finally:
                if telemetry_connection is not None:
                    telemetry_connection.close()
                if state_connection is not None:
                    state_connection.close()

        return self._run(operation)

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
        if not cursor or cursor == "0":
            return None
        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4))
            value = json.loads(raw)
            if (
                not isinstance(value, list)
                or len(value) != 2
                or not all(isinstance(item, str) and item for item in value)
            ):
                raise ValueError
            return value[0], value[1]
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise SafeError("invalid_cursor", "invalid history cursor") from exc

    @staticmethod
    def _encode_cursor(timestamp: str, row_id: str) -> str:
        raw = json.dumps([timestamp, row_id], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def _page_cursor(cls, rows: list[tuple[Any, ...]], limit: int) -> str | None:
        return cls._encode_cursor(str(rows[-1][1]), str(rows[-1][0])) if len(rows) == limit else None

    async def list_events(self, action: Any, actor: str | None = None, request_id: Any = None) -> EventPage:
        limit = int(action.page.limit)
        cursor = self._decode_cursor(action.page.cursor)
        predicate = "" if cursor is None else "WHERE (timestamp,id) < (?,?) "
        params: tuple[Any, ...] = () if cursor is None else cursor
        rows = await self._with_state_connection_async(
            lambda connection: connection.execute(
                f"SELECT id,timestamp,profile_id,code,message FROM events {predicate}"
                "ORDER BY timestamp DESC,id DESC LIMIT ?",
                params + (limit,),
            ).fetchall()
        )
        return EventPage(
            items=tuple(
                EventSummary(
                    id=str(row[0]),
                    timestamp=_timestamp(row[1]),
                    profile_id=ProfileId(row[2]) if row[2] else None,
                    code=str(row[3])[:64],
                    message=str(row[4])[:512],
                )
                for row in rows
            ),
            next_cursor=self._page_cursor(rows, limit),
        )

    async def list_audit(self, action: Any, actor: str | None = None, request_id: Any = None) -> AuditPage:
        limit = int(action.page.limit)
        cursor = self._decode_cursor(action.page.cursor)
        predicate = "" if cursor is None else "WHERE (timestamp,id) < (?,?) "
        params: tuple[Any, ...] = () if cursor is None else cursor
        rows = await self._with_state_connection_async(
            lambda connection: connection.execute(
                f"SELECT id,timestamp,actor,action,profile_id,result,error_code,detail FROM audit {predicate}"
                "ORDER BY timestamp DESC,id DESC LIMIT ?",
                params + (limit,),
            ).fetchall()
        )
        return AuditPage(
            items=tuple(
                AuditSummary(
                    id=str(row[0]),
                    timestamp=_timestamp(row[1]),
                    actor=str(row[2])[:128],
                    action=str(row[3])[:128],
                    profile_id=ProfileId(row[4]) if row[4] else None,
                    result=row[5],
                    error_code=_error_code(row[6]),
                    detail=str(row[7])[:512],
                )
                for row in rows
            ),
            next_cursor=self._page_cursor(rows, limit),
        )

    async def stats_summary(
        self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str
    ) -> dict[str, Any]:
        return await self._with_state_connection_async(
            lambda connection: query_stats_summary(
                connection, action.profile_id.value, action.days, hours=action.hours, now=now
            )
        )

    async def stats_heatmap(
        self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str
    ) -> dict[str, Any]:
        return await self._with_state_connection_async(
            lambda connection: query_stats_heatmap(
                connection, action.profile_id.value, action.days, hours=action.hours, now=now
            )
        )

    async def tps(
        self, action: Any, actor: str | None = None, request_id: Any = None, *, now: str
    ) -> dict[str, Any]:
        result = self._with_tps_connections(
            lambda state, telemetry: query_stats_tps(
                state,
                action.profile_id.value,
                action.window,
                now=now,
                telemetry=telemetry,
                resolution=action.resolution,
                limit=action.limit,
            )
        )
        return await result if inspect.isawaitable(result) else result

    def tps_sync(self, action: Any, *, now: str) -> dict[str, Any]:
        """Compatibility seam for synchronous direct service-group callers."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.tps(action, now=now))
        raise RuntimeError("tps_sync() cannot be called from a running event loop; use await tps()")

    async def _close_impl(self) -> None:
        with self._future_lock:
            futures = tuple(self._futures)
        # Consume every accepted future, including query failures, so worker
        # exceptions cannot become late/unretrieved warnings.
        if futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in futures),
                return_exceptions=True,
            )
        if self._owns_executor and not self._executor_closed:
            await asyncio.to_thread(
                self._executor.shutdown, wait=True, cancel_futures=True
            )
            self._executor_closed = True

    async def aclose(self) -> None:
        with self._state_lock:
            self._closing = True
            task = self._close_task
            if task is None or (task.done() and task.exception() is not None):
                task = asyncio.create_task(self._close_impl())
                self._close_task = task
        cancelled = False
        error: BaseException | None = None
        try:
            while True:
                try:
                    await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if task.done():
                        break
                except BaseException as exc:
                    error = exc
                    break
            if error is None and task.done():
                try:
                    task.result()
                except BaseException as exc:
                    error = exc
        except asyncio.CancelledError:
            cancelled = True
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException as exc:
                    error = exc
                    break
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    async def close(self) -> None:
        await self.aclose()


__all__ = ["HistoryQueryService"]
