import sqlite3
from datetime import datetime, timezone, timedelta
from uuid import uuid4

import pytest

from game_control.controller import Controller
from game_control.perf import PerformanceTracker
from game_control.protocol import GetPerf, GetStatus, PerfSnapshot, RpcRequest
from game_control import state_db


def test_tracker_keeps_rolling_samples_and_flushes_only_after_five_minutes():
    tracker = PerformanceTracker()
    for cycle, rpc in ((10.0, 2.0), (20.0, 4.0), (30.0, 6.0)):
        tracker.record_cycle(cycle)
        tracker.record_rpc(rpc)

    snapshot = tracker.snapshot()
    assert snapshot["cycle"]["count"] == 3
    assert snapshot["cycle"]["avg_ms"] == pytest.approx(20.0)
    assert snapshot["cycle"]["p95_ms"] == pytest.approx(29.0)
    assert snapshot["rpc"]["max_ms"] == pytest.approx(6.0)

    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
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
