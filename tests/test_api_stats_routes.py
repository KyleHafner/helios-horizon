from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.protocol import GetStatsHeatmap, GetStatsSummary, GetStatsTps, ListAggregateBackups, RpcSuccess
from game_control.web_main import create_app


HEADERS = {
    "X-Game-Control-Proxy": "secret",
    "X-authentik-username": "operator",
}


def test_stats_get_routes_construct_typed_actions():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={"ok": True})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))

    assert client.get("/api/v1/profiles/minecraft/stats/summary?days=7", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/profiles/minecraft/stats/heatmap?days=14", headers=HEADERS).status_code == 200
    assert client.get("/api/v1/profiles/minecraft/stats/tps?window=1h", headers=HEADERS).status_code == 200

    assert isinstance(calls[0], GetStatsSummary)
    assert calls[0].days == 7
    assert isinstance(calls[1], GetStatsHeatmap)
    assert calls[1].days == 14
    assert isinstance(calls[2], GetStatsTps)
    assert calls[2].window == "1h"


def test_stats_tps_rejects_invalid_window():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/profiles/minecraft/stats/tps?window=2h", headers=HEADERS)

    assert response.status_code == 422
    assert not calls


def test_stats_tps_resolution_and_limit_are_typed_and_bounded():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get(
        "/api/v1/profiles/minecraft/stats/tps?window=7d&resolution=1h&limit=2000", headers=HEADERS
    )
    assert response.status_code == 200
    assert calls[0].window == "7d" and calls[0].resolution == "1h" and calls[0].limit == 2000
    assert client.get(
        "/api/v1/profiles/minecraft/stats/tps?resolution=2m", headers=HEADERS
    ).status_code == 422
    assert client.get(
        "/api/v1/profiles/minecraft/stats/tps?limit=2001", headers=HEADERS
    ).status_code == 422


@pytest.mark.parametrize("window,hours", [("1h", 1), ("6h", 6), ("24h", 24)])
def test_stats_window_routes_preserve_exact_hour_volume(window, hours):
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    headers = {**HEADERS}
    assert client.get(f"/api/v1/profiles/minecraft/stats/summary?hours={hours}", headers=headers).status_code == 200
    assert client.get(f"/api/v1/profiles/minecraft/stats/heatmap?hours={hours}", headers=headers).status_code == 200
    assert calls[0].hours == hours and calls[1].hours == hours


def test_stats_routes_require_authenticated_session():
    client = TestClient(create_app(proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/profiles/minecraft/stats/summary")
    assert response.status_code in {401, 403}


def test_aggregate_backups_route_is_one_bounded_typed_read():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={"items": [], "next_cursor": None})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/backups?limit=500", headers=HEADERS)
    assert response.status_code == 200
    assert len(calls) == 1 and isinstance(calls[0], ListAggregateBackups)
    assert calls[0].page.limit == 500
