from uuid import uuid4

from fastapi.testclient import TestClient

from game_control.protocol import GetProfileConfig, RpcSuccess, SetProfileConfig
from game_control.web_main import create_app


HEADERS = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}


def test_config_routes_construct_typed_actions():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={"ok": True})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"), base_url="https://games.example.com")
    assert client.get("/api/v1/profiles/minecraft/config", headers=HEADERS).status_code == 200
    session = client.get("/api/v1/session", headers=HEADERS)
    mutation_headers = {**HEADERS, "X-CSRF-Token": session.json()["csrf_token"], "Origin": "https://games.example.com"}
    assert client.post("/api/v1/profiles/minecraft/config", json={"changes": {"motd": "Hello"}}, headers=mutation_headers).status_code == 200
    assert isinstance(calls[0], GetProfileConfig)
    assert isinstance(calls[1], SetProfileConfig)
    assert calls[1].changes == {"motd": "Hello"}
