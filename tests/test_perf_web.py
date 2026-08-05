from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.protocol import GetPerf, PerfSnapshot, RpcSuccess, StatusSnapshot
from game_control.web_main import BoundedTimingRing, EventHub, create_app


HEADERS = {
    "X-Game-Control-Proxy": "secret",
    "X-authentik-username": "operator",
}


def test_bounded_timing_ring_keeps_only_recent_samples_and_computes_percentiles():
    ring = BoundedTimingRing(maxlen=3)
    for value in (1.0, 2.0, 3.0, 4.0):
        ring.record(value)

    stats = ring.snapshot()

    assert stats["count"] == 3
    assert stats["p50_ms"] == pytest.approx(3.0)
    assert stats["p95_ms"] == pytest.approx(3.9)
    assert stats["max_ms"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_event_hub_reports_connected_clients_and_publish_flush_lag():
    hub = EventHub(max_history=4)
    client = await hub.subscribe()

    item = await hub.publish("status", {"generation": 1})
    queued = await client.queue.get()
    assert queued["id"] == item["id"]
    hub.record_flush(queued)

    stats = hub.perf_snapshot()
    assert stats["connected_clients"] == 1
    assert stats["publish_flush_lag_ms"]["count"] == 1


def test_authenticated_perf_endpoint_reports_route_and_rpc_timing():
    async def rpc(_actor, _action):
        if isinstance(_action, GetPerf):
            return RpcSuccess(
                request_id=uuid4(),
                result=PerfSnapshot(
                    cycle={"count": 1, "avg_ms": 2.0, "p95_ms": 2.0, "max_ms": 2.0},
                    rpc={"count": 1, "avg_ms": 1.0, "p95_ms": 1.0, "max_ms": 1.0},
                ),
            )
        return RpcSuccess(
            request_id=uuid4(),
            result=StatusSnapshot(
                generation=0,
                observed_at="2026-01-01T00:00:00Z",
                profiles=(),
            ),
        )

    client = TestClient(
        create_app(
            rpc=rpc,
            proxy_credential="secret",
            session_db=":memory:",
            web_root=Path(__file__).resolve().parents[1] / "web",
        )
    )

    assert client.get("/api/v1/perf").status_code in {401, 403}
    session = client.get("/api/v1/session", headers=HEADERS)
    assert session.status_code == 200
    response = client.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200

    perf = client.get("/api/v1/perf", headers=HEADERS)

    assert perf.status_code == 200
    body = perf.json()
    assert body["GET /api/v1/status"]["count"] >= 1
    assert body["rpc"]["count"] >= 1
    assert body["sse"]["connected_clients"] == 0
    assert body["slotd"]["cycle"]["p95_ms"] == 2.0
