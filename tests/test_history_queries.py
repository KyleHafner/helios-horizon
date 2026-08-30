from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control.errors import SafeError
from game_control.history_queries import HistoryQueryService
from game_control.models import ProfileId
from game_control.protocol import (
    GetStatsHeatmap,
    GetStatsSummary,
    GetStatsTps,
    ListAudit,
    ListEvents,
    PageOptions,
)


def _state_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                           profile_id TEXT, code TEXT NOT NULL, message TEXT NOT NULL);
        CREATE TABLE audit(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL,
                           actor TEXT NOT NULL, action TEXT NOT NULL, profile_id TEXT,
                           result TEXT NOT NULL, error_code TEXT, detail TEXT NOT NULL);
        CREATE TABLE player_sessions(id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                           player TEXT NOT NULL, started_at TEXT NOT NULL,
                           ended_at TEXT, source TEXT NOT NULL);
        CREATE TABLE metric_samples(profile_id TEXT NOT NULL, metric TEXT NOT NULL,
                           ts TEXT NOT NULL, value REAL NOT NULL);
        INSERT INTO events VALUES ('e1', '2026-08-29T00:00:00Z', NULL, 'tick', 'ok');
        INSERT INTO audit VALUES ('a1', '2026-08-29T00:00:00Z', 'system', 'start', NULL, 'succeeded', NULL, 'ok');
        """
    )
    connection.commit()
    return connection


@pytest.mark.asyncio
async def test_history_uses_owner_thread_for_explicit_in_memory_connection():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE events(id TEXT, timestamp TEXT, profile_id TEXT, code TEXT, message TEXT);"
        "INSERT INTO events VALUES ('e1','2026-08-29T00:00:00Z',NULL,'tick','ok');"
    )
    owner_thread = threading.get_ident()
    seen: list[int] = []
    connection.set_trace_callback(lambda _sql: seen.append(threading.get_ident()))
    service = HistoryQueryService(connection)
    page = await service.list_events(ListEvents(kind="list_events", page=PageOptions(limit=10)))
    assert [item.id for item in page.items] == ["e1"]
    assert seen and set(seen) == {owner_thread}
    await service.aclose()
    connection.close()


@pytest.mark.asyncio
async def test_file_query_is_read_only_and_never_borrows_writer(tmp_path):
    path = tmp_path / "state.db"
    writer = _state_db(path)
    queries: list[str] = []
    writer.set_trace_callback(queries.append)

    class Database:
        pass

    database = Database()
    database.path = path
    database.connection = writer
    service = HistoryQueryService(database, approved_state_path=path)
    page = await service.list_audit(ListAudit(kind="list_audit", page=PageOptions(limit=10)))
    assert [item.id for item in page.items] == ["a1"]
    assert queries == []
    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            readonly.execute("INSERT INTO events VALUES ('x','2026-08-29T00:00:00Z',NULL,'x','x')")
    finally:
        readonly.close()
    await service.aclose()
    writer.close()


@pytest.mark.asyncio
async def test_wrong_path_fails_closed_before_any_worker_open(tmp_path):
    wrong = tmp_path / "wrong.db"
    service = HistoryQueryService(SimpleNamespace(path=wrong), approved_state_path=tmp_path / "approved.db")
    with pytest.raises(SafeError) as raised:
        await service.list_events(ListEvents(kind="list_events", page=PageOptions()))
    assert raised.value.code == "state_unavailable"
    await service.aclose()

    approved = tmp_path / "state.db"
    telemetry = SimpleNamespace(path=wrong)
    service = HistoryQueryService(
        SimpleNamespace(path=approved), telemetry,
        approved_state_path=approved,
    )
    with pytest.raises(SafeError) as raised:
        await service.tps(GetStatsTps(kind="get_stats_tps", profile_id=ProfileId.MINECRAFT), now="2026-08-29T00:00:00Z")
    assert raised.value.code == "state_unavailable"
    await service.aclose()


@pytest.mark.asyncio
async def test_all_four_stats_operations_share_one_owner(monkeypatch):
    connection = sqlite3.connect(":memory:")
    service = HistoryQueryService(connection)
    calls: list[str] = []
    monkeypatch.setattr("game_control.history_queries.query_stats_summary", lambda *_args, **_kwargs: calls.append("summary") or {"kind": "summary"})
    monkeypatch.setattr("game_control.history_queries.query_stats_heatmap", lambda *_args, **_kwargs: calls.append("heatmap") or {"kind": "heatmap"})
    monkeypatch.setattr("game_control.history_queries.query_stats_tps", lambda *_args, **kwargs: calls.append("tps") or {"telemetry": kwargs.get("telemetry")})
    profile = ProfileId.MINECRAFT
    await service.stats_summary(GetStatsSummary(kind="get_stats_summary", profile_id=profile, days=1), now="2026-08-29T00:00:00Z")
    await service.stats_heatmap(GetStatsHeatmap(kind="get_stats_heatmap", profile_id=profile, days=1), now="2026-08-29T00:00:00Z")
    result = await service.tps(GetStatsTps(kind="get_stats_tps", profile_id=profile), now="2026-08-29T00:00:00Z")
    assert calls == ["summary", "heatmap", "tps"]
    assert result == {"telemetry": None}
    await service.aclose()
    connection.close()


def test_tps_sync_rejects_running_loop_and_preserves_outside_loop(monkeypatch):
    connection = sqlite3.connect(":memory:")
    service = HistoryQueryService(connection)
    monkeypatch.setattr("game_control.history_queries.query_stats_tps", lambda *_args, **_kwargs: {"ok": True})
    action = GetStatsTps(kind="get_stats_tps", profile_id=ProfileId.MINECRAFT)
    assert service.tps_sync(action, now="2026-08-29T00:00:00Z") == {"ok": True}
    async def inside() -> None:
        with pytest.raises(RuntimeError, match="running event loop"):
            service.tps_sync(action, now="2026-08-29T00:00:00Z")
        await service.aclose()
    asyncio.run(inside())
    connection.close()


@pytest.mark.asyncio
async def test_owned_executor_is_bounded_and_borrowed_executor_survives_close(tmp_path):
    path = tmp_path / "state.db"
    writer = _state_db(path)
    database = SimpleNamespace(path=path, connection=writer)
    service = HistoryQueryService(database, approved_state_path=path)
    assert service._executor._max_workers == 4
    await service.list_events(ListEvents(kind="list_events", page=PageOptions()))
    await service.aclose()
    await service.aclose()
    writer.close()

    borrowed = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    service = HistoryQueryService(sqlite3.connect(":memory:"), executor=borrowed)
    await service.aclose()
    assert borrowed.submit(lambda: 1).result() == 1
    borrowed.shutdown()


@pytest.mark.asyncio
async def test_close_drains_cancelled_query_and_consumes_late_failure():
    service = HistoryQueryService(sqlite3.connect(":memory:"))
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    prior_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    started = threading.Event()
    release = threading.Event()

    def worker() -> None:
        started.set()
        release.wait(2)
        raise RuntimeError("late worker failure")

    query = asyncio.create_task(service._run(worker))
    await asyncio.to_thread(started.wait, 2)
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    close = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    close.cancel()
    close.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    await service.aclose()
    loop.set_exception_handler(prior_handler)
    assert loop_errors == []
