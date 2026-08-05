from uuid import uuid4

from fastapi.testclient import TestClient

from game_control.protocol import GetStatsHeatmap, GetStatsSummary, GetStatsTps, RpcSuccess
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
    response = client.get("/api/v1/profiles/minecraft/stats/tps?window=7d", headers=HEADERS)

    assert response.status_code == 422
    assert not calls


def test_stats_routes_require_authenticated_session():
    client = TestClient(create_app(proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/profiles/minecraft/stats/summary")
    assert response.status_code in {401, 403}
