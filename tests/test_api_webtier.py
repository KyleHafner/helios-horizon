from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.protocol import (
    BenchmarkOverview,
    ErrorCode,
    GetBenchmarks,
    JobAccepted,
    RpcError,
    RpcFailure,
    RpcSuccess,
    RunBenchmark,
    StatusSnapshot,
)
from game_control.models import ProfileId
from game_control.web_main import create_app


PROXY_HEADERS = {
    "X-Game-Control-Proxy": "secret",
    "X-authentik-username": "operator",
}
ORIGIN = "https://games.example.com"


def _status_result():
    return StatusSnapshot(generation=0, observed_at="2026-01-01T00:00:00Z", profiles=())


def _authenticated_client(rpc):
    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url=ORIGIN,
    )
    session = client.get("/api/v1/session", headers=PROXY_HEADERS)
    assert session.status_code == 200
    return client, {
        **PROXY_HEADERS,
        "X-CSRF-Token": session.json()["csrf_token"],
        "Origin": ORIGIN,
    }


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/v1/profiles/minecraft/start", {"unexpected": True}),
        ("POST", "/api/v1/profiles/minecraft/command", {"command": "say hi", "unexpected": True}),
        ("POST", "/api/v1/profiles/minecraft/notifications/test", {"channel": "sms"}),
        ("PATCH", "/api/v1/profiles/minecraft/idle-stop", {"minutes": 3}),
        ("POST", "/api/v1/switch/prepare", {"current_profile_id": "minecraft"}),
        (
            "POST",
            "/api/v1/world-clone/prepare",
            {"source_world_id": "world", "destination_name": "../escape"},
        ),
        ("POST", "/api/v1/schedules", {"entries": [{"cron": "short", "profile": "minecraft"}]}),
        (
            "POST",
            "/api/v1/profiles/minecraft-sunlit-cobblemon/benchmarks",
            {"baseline_preset": "same", "candidate_preset": "same"},
        ),
    ],
)
def test_route_validation_rejects_invalid_mutations_before_rpc(method, path, payload):
    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(request_id=uuid4(), result=_status_result())

    client, headers = _authenticated_client(rpc)
    response = client.request(method, path, json=payload, headers=headers)

    assert response.status_code == 422
    assert not calls


def test_route_rejects_malformed_and_non_object_mutation_bodies_before_rpc():
    calls = []

    async def rpc(actor, action):
        calls.append((actor, action))
        return RpcSuccess(request_id=uuid4(), result=_status_result())

    client, headers = _authenticated_client(rpc)
    malformed = client.post(
        "/api/v1/profiles/minecraft/command",
        content=b"{",
        headers={**headers, "Content-Type": "application/json"},
    )
    non_object = client.post(
        "/api/v1/profiles/minecraft/command",
        json=["say hi"],
        headers=headers,
    )

    assert malformed.status_code == 422
    assert non_object.status_code == 422
    assert not calls


def test_benchmark_routes_project_only_typed_preset_ids():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        if isinstance(action, GetBenchmarks):
            result = BenchmarkOverview(
                profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
                available=True,
                presets=(),
                runs=(),
            )
        else:
            result = JobAccepted(job_id="benchmark-job", state="accepted")
        return RpcSuccess(request_id=uuid4(), result=result)

    client, headers = _authenticated_client(rpc)
    fetched = client.get(
        "/api/v1/profiles/minecraft-sunlit-cobblemon/benchmarks",
        headers=PROXY_HEADERS,
    )
    started = client.post(
        "/api/v1/profiles/minecraft-sunlit-cobblemon/benchmarks",
        json={"baseline_preset": "current", "candidate_preset": "candidate"},
        headers=headers,
    )

    assert fetched.status_code == 200 and fetched.json()["available"] is True
    assert started.status_code == 200 and started.json()["job_id"] == "benchmark-job"
    assert isinstance(calls[0], GetBenchmarks)
    assert isinstance(calls[1], RunBenchmark)
    assert calls[1].baseline_preset == "current"


def test_auth_and_csrf_are_enforced_at_mutation_route_layer():
    async def rpc(actor, action):
        return RpcSuccess(request_id=uuid4(), result=_status_result())

    fresh = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url=ORIGIN,
    )
    no_session = fresh.post(
        "/api/v1/profiles/minecraft/start",
        headers={**PROXY_HEADERS, "Origin": ORIGIN},
    )
    assert no_session.status_code == 401

    client, headers = _authenticated_client(rpc)
    no_csrf = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**PROXY_HEADERS, "Origin": ORIGIN},
    )
    wrong_origin = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "Origin": "https://evil.example"},
    )

    assert no_csrf.status_code == 403
    assert wrong_origin.status_code == 403


@pytest.mark.parametrize(
    ("code", "retryable", "expected_status"),
    [
        (ErrorCode.SLOT_CONFLICT, True, 409),
        (ErrorCode.CONFIRMATION_EXPIRED, False, 410),
        (ErrorCode.CONFIRMATION_MISMATCH, False, 409),
        (ErrorCode.UNAUTHORIZED_PEER, False, 403),
        (ErrorCode.INVALID_REQUEST, False, 400),
        (ErrorCode.PROFILE_RESERVED, False, 400),
        (ErrorCode.UPSTREAM_UNAVAILABLE, True, 503),
    ],
)
def test_typed_rpc_errors_map_to_stable_http_codes(code, retryable, expected_status):
    async def rpc(actor, action):
        return RpcFailure(
            request_id=uuid4(),
            error=RpcError(code=code, message="safe failure", retryable=retryable),
        )

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/status", headers=PROXY_HEADERS)

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {
            "code": code.value,
            "message": "safe failure",
            "retryable": retryable,
            "details": None,
        }
    }


def test_rpc_exception_maps_to_redacted_upstream_unavailable_error():
    async def rpc(actor, action):
        raise RuntimeError("secret socket path and credentials")

    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))
    response = client.get("/api/v1/status", headers=PROXY_HEADERS)

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "upstream_unavailable",
            "message": "control service unavailable",
            "retryable": True,
            "details": None,
        }
    }


@pytest.mark.parametrize(
    "query",
    [
        "limit=not-a-number",
        "limit=0",
        "limit=501",
        "severity=not-a-severity",
        "days=0",
        "window=2h",
    ],
)
def test_route_query_validation_rejects_invalid_pagination_and_windows(query):
    async def rpc(actor, action):
        raise AssertionError("invalid query must not reach RPC")

    path = "/api/v1/events" if query.startswith("limit=") else "/api/v1/profiles/minecraft/logs"
    if query.startswith("window="):
        path = "/api/v1/profiles/minecraft/stats/tps"
    if query.startswith("days="):
        path = "/api/v1/profiles/minecraft/stats/summary"
    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))

    response = client.get(f"{path}?{query}", headers=PROXY_HEADERS)

    assert response.status_code == 422
