from uuid import uuid4

from fastapi.testclient import TestClient

from game_control.protocol import GetSchedules, RpcSuccess, SetSchedules
from game_control.web_main import create_app


HEADERS = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}


def test_schedule_routes_construct_typed_actions_and_forbid_unknown_fields():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={"schedules": []})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"), base_url="https://games.example.com")
    assert client.get("/api/v1/schedules", headers=HEADERS).status_code == 200
    session = client.get("/api/v1/session", headers=HEADERS)
    mutation_headers = {**HEADERS, "X-CSRF-Token": session.json()["csrf_token"], "Origin": "https://games.example.com"}
    assert client.post(
        "/api/v1/schedules",
        json={"entries": [{"cron": "0 20 * * 5", "profile": "minecraft"}]},
        headers=mutation_headers,
    ).status_code == 200
    assert isinstance(calls[0], GetSchedules)
    assert isinstance(calls[1], SetSchedules)
    assert calls[1].entries[0].profile == "minecraft"
    assert calls[1].entries[0].enabled is True
    assert client.post(
        "/api/v1/schedules",
        json={"entries": [{"cron": "* * * * *", "profile": "minecraft", "enabled": "false"}]},
        headers=mutation_headers,
    ).status_code == 422
