import asyncio
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from game_control.controller import Controller
from game_control.perf import PerformanceTracker
from game_control.db_telemetry import collect_database_telemetry
import game_control.db_telemetry as db_telemetry
from game_control.protocol import GetPerf, GetStatus, PerfSnapshot, RpcRequest
from game_control import state_db


@pytest.fixture
def connection():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    yield connection
    connection.close()


def test_tracker_keeps_rolling_samples_and_flushes_only_after_five_minutes(connection):
    tracker = PerformanceTracker()
    for cycle, rpc in ((10.0, 2.0), (20.0, 4.0), (30.0, 6.0)):
        tracker.record_cycle(cycle)
        tracker.record_rpc(rpc)

    snapshot = tracker.snapshot()
    assert snapshot["cycle"]["count"] == 3
    assert snapshot["cycle"]["avg_ms"] == pytest.approx(20.0)
    assert snapshot["cycle"]["p95_ms"] == pytest.approx(29.0)
    assert snapshot["rpc"]["max_ms"] == pytest.approx(6.0)

    start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert tracker.flush_if_due(connection, now=start) == 0
    assert tracker.flush_if_due(connection, now=start + timedelta(minutes=4, seconds=59)) == 0
    assert tracker.flush_if_due(connection, now=start + timedelta(minutes=5)) == 6

    rows = connection.execute(
        "SELECT profile_id, metric, value FROM metric_samples ORDER BY metric"
    ).fetchall()
    assert len(rows) == 6
    assert {row[0] for row in rows} == {"slotd"}
    assert {row[1] for row in rows} == {
        "perf.cycle_avg_ms",
        "perf.cycle_p95_ms",
        "perf.cycle_max_ms",
        "perf.rpc_avg_ms",
        "perf.rpc_p95_ms",
        "perf.rpc_max_ms",
    }
    assert tracker.flush_if_due(connection, now=start + timedelta(minutes=6)) == 0


def test_tracker_exposes_bounded_identity_free_maintenance_ring():
    tracker = PerformanceTracker(maxlen=2)
    tracker.record_maintenance(3.0)
    tracker.record_maintenance(5.0)
    tracker.record_maintenance(7.0)
    snapshot = tracker.snapshot()
    assert snapshot["maintenance"] == {"count": 2, "avg_ms": 6.0, "p95_ms": 6.9, "max_ms": 7.0}
    assert snapshot["maintenance_ms"] == [5.0, 7.0]
    assert snapshot["maintenance_sequence"] == {"start": 1, "end": 3}
    assert all("pid" not in key for key in snapshot)


def test_database_telemetry_reports_bounded_growth_and_oldest_rows(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    state_db._configure(db)
    state_db._migrate_state(db)
    db.execute("INSERT INTO events VALUES ('opaque', '2026-01-02T00:00:00Z', NULL, 'test', 'message')")
    db.commit()

    telemetry = collect_database_telemetry(type("Database", (), {"connection": db, "path": path})(), "state")

    assert telemetry["state"] == "available"
    assert telemetry["page_count"] >= 1 and telemetry["page_size"] > 0
    events = next(item for item in telemetry["tables"] if item["name"] == "events")
    assert events == {"name": "events", "row_count": 1, "oldest_timestamp": "2026-01-02T00:00:00Z"}
    assert telemetry["query_ms"]["count"] == 13
    assert "opaque" not in str(telemetry)
    db.close()


def test_database_telemetry_inactive_database_is_typed_and_empty():
    telemetry = collect_database_telemetry(None, "telemetry")

    assert telemetry == {
        "state": "inactive", "tables": (), "page_count": None, "page_size": None,
        "freelist_pages": None, "wal_bytes": None,
        "query_ms": {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None},
    }


@pytest.mark.asyncio
async def test_path_database_telemetry_does_not_block_event_loop(monkeypatch, tmp_path):
    def slow_collect(_database, _kind):
        time.sleep(0.08)
        return {"state": "inactive", "tables": (), "page_count": None, "page_size": None,
                "freelist_pages": None, "wal_bytes": None,
                "query_ms": {"count": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}}

    monkeypatch.setattr(db_telemetry, "collect_database_telemetry", slow_collect)
    database = type("Database", (), {"path": tmp_path / "state.db"})()
    task = asyncio.create_task(db_telemetry.collect_database_telemetry_async(database, "state"))
    ticks = 0
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.005)
    await task
    assert ticks >= 3


@pytest.mark.asyncio
async def test_get_perf_is_a_read_only_controller_action(tmp_path):
    controller = Controller.for_testing(tmp_path)
    await controller.execute(RpcRequest(request_id=uuid4(), actor="operator", action=GetStatus(kind="get_status")))
    request = RpcRequest(request_id=uuid4(), actor="operator", action=GetPerf(kind="get_perf"))

    response = await controller.execute(request)

    assert response.ok is True
    assert isinstance(response.result, PerfSnapshot)
    assert response.result.cycle.count >= 1
    assert response.result.rpc.count >= 1
