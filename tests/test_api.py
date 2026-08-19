from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from game_control.api import ROUTE_ACTIONS
from game_control.protocol import GetStatus, JobAccepted, RpcSuccess, StatusSnapshot
from game_control.protocol import ErrorCode, GetLogs, RpcError, RpcFailure
from starlette.requests import Request

from game_control.web_main import create_app
from game_control.web_main import AdaptiveStatusCadence


def test_successful_mutation_wakes_idle_status_publisher():
    import threading
    import time

    status_calls = 0
    first_status = threading.Event()
    second_status = threading.Event()

    async def rpc(actor, action):
        nonlocal status_calls
        if isinstance(action, GetStatus):
            status_calls += 1
            (first_status if status_calls == 1 else second_status).set()
            return RpcSuccess(
                request_id=uuid4(),
                result=StatusSnapshot(generation=0, observed_at="2026-01-01T00:00:00Z", profiles=()),
            )
        return RpcSuccess(
            request_id=uuid4(),
            result=JobAccepted(job_id=str(uuid4()), state="running"),
        )

    app = create_app(
        rpc=rpc,
        proxy_credential="secret",
        session_db=":memory:",
        status_cadence=AdaptiveStatusCadence(fast_interval=0.05, idle_interval=1.0),
    )
    headers = {
        "X-Game-Control-Proxy": "secret",
        "X-authentik-username": "operator",
    }
    with TestClient(app, base_url="https://games.example.com") as client:
        assert first_status.wait(0.5)
        session = client.get("/api/v1/session", headers=headers)
        csrf = session.json()["csrf_token"]
        started = time.monotonic()
        response = client.post(
            "/api/v1/profiles/minecraft/start",
            headers={**headers, "X-CSRF-Token": csrf, "Origin": "https://games.example.com"},
        )
        assert response.status_code == 200
        assert second_status.wait(0.5)
        assert time.monotonic() - started < 0.8


def test_status_publisher_backs_off_after_repeated_rpc_failures():
    import threading
    import time

    calls: list[float] = []
    fourth_call = threading.Event()

    async def rpc(actor, action):
        if isinstance(action, GetStatus):
            calls.append(time.monotonic())
            if len(calls) >= 4:
                fourth_call.set()
            raise RuntimeError("slotd unavailable")
        raise AssertionError("unexpected mutation")

    app = create_app(
        rpc=rpc,
        proxy_credential="secret",
        session_db=":memory:",
        status_cadence=AdaptiveStatusCadence(fast_interval=0.1, idle_interval=0.35),
    )
    with TestClient(app) as client:
        assert fourth_call.wait(1.5)
        assert calls[2] - calls[1] < 0.25
        assert calls[3] - calls[2] >= 0.3


def test_web_app_serves_dashboard_assets():
    client = TestClient(
        create_app(
            proxy_credential="secret",
            session_db=":memory:",
            web_root=Path(__file__).resolve().parents[1] / "web",
        )
    )

    index = client.get("/")
    script = client.get("/app.js")
    stylesheet = client.get("/styles.css")

    assert index.status_code == 200
    assert '<main id="main-content"' in index.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "const PROFILE_FALLBACK" in script.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ".active-slot" in stylesheet.text
    assert 'id="conn-state"' in index.text
    assert 'id="tab-benchmarks"' in index.text
    assert "function renderBenchmarks" in script.text
    assert ".benchmark-verdict" in stylesheet.text


def test_production_origin_allows_authenticated_mutation():
    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(
            request_id=uuid4(),
            result=JobAccepted(job_id=str(uuid4()), state="running"),
        )

    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    headers = {
        "X-Game-Control-Proxy": "secret",
        "X-authentik-username": "operator",
    }
    session = client.get("/api/v1/session", headers=headers)
    csrf = session.json()["csrf_token"]
    response = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={
            **headers,
            "X-CSRF-Token": csrf,
            "Origin": "https://games.example.com",
        },
    )

    assert response.status_code == 200
    assert calls and calls[0][0] == "operator"


def test_returning_session_bootstrap_rotates_csrf_for_mutation():
    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(
            request_id=uuid4(),
            result=JobAccepted(job_id=str(uuid4()), state="running"),
        )

    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    headers = {
        "X-Game-Control-Proxy": "secret",
        "X-authentik-username": "operator",
    }

    first = client.get("/api/v1/session", headers=headers)
    assert first.status_code == 200
    assert first.json()["csrf_token"]

    returning = client.get("/api/v1/session", headers=headers)
    csrf = returning.json()["csrf_token"]
    assert returning.status_code == 200
    assert csrf

    response = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "X-CSRF-Token": csrf, "Origin": "https://games.example.com"},
    )

    assert response.status_code == 200
    assert calls and calls[0][0] == "operator"


def test_sse_response_starts_with_retry_hint(monkeypatch):
    async def disconnected(_request):
        return True

    monkeypatch.setattr(Request, "is_disconnected", disconnected)

    async def rpc(actor, action):
        return RpcSuccess(
            request_id=uuid4(),
            result=StatusSnapshot(generation=0, observed_at="2026-01-01T00:00:00Z", profiles=()),
        )

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    headers = {"X-Game-Control-Proxy": "secret", "X-Authentik-Username": "operator"}
    with client.stream("GET", "/api/v1/stream", headers=headers) as response:
        body = b"".join(response.iter_bytes())
    assert body.startswith(b"retry: 3000\n\n")


def test_route_map_is_closed_and_rpc_only():
    assert ROUTE_ACTIONS["GET /api/v1/status"] is GetStatus
    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(request_id=uuid4(), result=StatusSnapshot(generation=0, observed_at="2026-01-01T00:00:00Z", profiles=()))

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/status", headers={"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"})
    assert response.status_code == 200
    assert isinstance(calls[0][1], GetStatus)


def test_unknown_body_fields_are_rejected():
    client = TestClient(create_app(proxy_credential="secret", session_db=":memory:"))
    response = client.post("/api/v1/profiles/minecraft/start", json={"command": "rm -rf /"}, headers={"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"})
    assert response.status_code in {400, 401, 403, 422}


def test_slot_conflict_is_stable_and_pagination_is_bounded():
    async def rpc(actor, action):
        return RpcFailure(
            request_id=uuid4(),
            error=RpcError(code=ErrorCode.SLOT_CONFLICT, message="slot is occupied", retryable=True),
        )

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get(
        "/api/v1/events?limit=999999",
        headers={"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"},
    )
    assert response.status_code == 422


def test_logs_reject_naive_datetime_range():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcSuccess(request_id=uuid4(), result={"items": [], "next_cursor": None})

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get(
        "/api/v1/profiles/terraria-tmod/logs?since=2026-07-13T12:00:00&limit=10",
        headers={"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"},
    )
    assert response.status_code == 422
    assert not calls
