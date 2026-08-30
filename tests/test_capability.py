from __future__ import annotations

import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from starlette.requests import Request
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.capability import (
    CAPABILITY_START_PROFILE,
    CapabilityAudience,
    CapabilityError,
    CapabilityRole,
    CapabilityService,
    CapabilityTokenStore,
)
from game_control.models import HealthState, ObservedState, ProfileId
from game_control.protocol import (
    ErrorCode,
    GetStatus,
    ProfileStatus,
    RpcError,
    RpcFailure,
    RpcSuccess,
    ReadinessResult,
    Start,
    StatusSnapshot,
    WaitReadiness,
)
from game_control.web_main import create_app


def _snapshot():
    return StatusSnapshot(
        generation=4,
        observed_at="2026-08-06T12:00:00Z",
        profiles=(
            ProfileStatus(
                profile_id=CAPABILITY_START_PROFILE,
                state=ObservedState.RUNNING,
                health=HealthState.HEALTHY,
                slot_owner=CAPABILITY_START_PROFILE,
                active_job_id=None,
                pid=123,
                started_at="2026-08-06T11:00:00Z",
                uptime_seconds=3600,
                cpu_percent=1.0,
                rss_bytes=10,
                players_online=1,
                installed_version="pinned",
                restart_required=False,
                required_ports_ready=True,
            ),
        ),
    )


def _stopped_snapshot():
    snapshot = _snapshot()
    return snapshot.model_copy(
        update={
            "profiles": (
                snapshot.profiles[0].model_copy(
                    update={
                        "state": ObservedState.STOPPED,
                        "health": HealthState.UNKNOWN,
                        "slot_owner": None,
                        "pid": None,
                        "started_at": None,
                        "uptime_seconds": None,
                        "players_online": None,
                        "required_ports_ready": False,
                    }
                ),
            )
        }
    )


class _Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


def _store(clock=None):
    return CapabilityTokenStore(sqlite3.connect(":memory:", check_same_thread=False), clock=clock)


def test_issue_is_show_once_hashed_and_bound_to_exact_role_audience():
    store = _store()
    issued = store.issue(role=CapabilityRole.WAKER, audience=CapabilityAudience.LAZYMC, profile_id=CAPABILITY_START_PROFILE)
    assert issued.token.startswith("hc_")
    assert issued.scopes == frozenset({"status", "wake"})
    assert issued.token not in repr(issued)
    row = store.db.execute("SELECT token_hash,role,audience,profile_id FROM capability_tokens").fetchone()
    assert issued.token not in row
    assert row[1:] == ("waker", "lazymc", CAPABILITY_START_PROFILE.value)


def test_observer_and_waker_scope_enforcement_and_no_profile_input():
    store = _store()
    observer = store.issue(role="observer", audience="helios-mcp")
    waker = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        return RpcSuccess(request_id=request.request_id, result=_snapshot())

    service = CapabilityService(store, rpc)
    observer_response = __import__("asyncio").run(
        service.handle(observer.token, observer.audience, service.parse_request({"request_id": str(uuid4()), "action": {"kind": "tps"}}))
    )
    assert observer_response.status == 200
    waker_response = __import__("asyncio").run(
        service.handle(waker.token, waker.audience, service.parse_request({"request_id": str(uuid4()), "action": {"kind": "tps"}}))
    )
    assert waker_response.status == 403
    assert len(calls) == 1
    try:
        service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake", "profile_id": "minecraft"}})
    except Exception as exc:
        assert getattr(exc, "code", None) == "invalid_request"
    else:
        raise AssertionError("profile input must be rejected")


def test_mcp_waker_is_separate_from_observer_and_lazymc_audience():
    store = _store()
    observer = store.issue(role="observer", audience="helios-mcp")
    mcp_waker = store.issue(role="waker", audience="helios-mcp", profile_id=CAPABILITY_START_PROFILE)
    lazymc_waker = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)

    assert observer.scopes == frozenset({"status", "tps"})
    assert mcp_waker.scopes == lazymc_waker.scopes == frozenset({"status", "wake"})
    assert mcp_waker.audience is CapabilityAudience.HELIOS_MCP
    assert lazymc_waker.audience is CapabilityAudience.LAZYMC
    assert store._grant(mcp_waker.token, CapabilityAudience.HELIOS_MCP).profile_id is CAPABILITY_START_PROFILE
    with pytest.raises(CapabilityError) as mcp_wrong_audience:
        store._grant(mcp_waker.token, CapabilityAudience.LAZYMC)
    assert mcp_wrong_audience.value.code == "audience_mismatch"
    with pytest.raises(CapabilityError) as lazymc_wrong_audience:
        store._grant(lazymc_waker.token, CapabilityAudience.HELIOS_MCP)
    assert lazymc_wrong_audience.value.code == "audience_mismatch"

    with pytest.raises(CapabilityError):
        CapabilityService.parse_request({"request_id": str(uuid4()), "action": {"kind": "stop"}})


def test_wake_is_idempotent_conflict_safe_and_never_confirms():
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE, rate_budget=2)
    calls = []

    async def rpc(request):
        calls.append(request)
        if isinstance(request.action, GetStatus):
            return RpcSuccess(request_id=request.request_id, result=_stopped_snapshot())
        return RpcSuccess(request_id=request.request_id, result={
            "job_id": "safe-job", "state": "running",
            "readiness_generation": 4, "readiness": "success",
        })

    service = CapabilityService(store, rpc)
    request_id = str(uuid4())
    request = service.parse_request({"request_id": request_id, "action": {"kind": "wake"}})
    first = __import__("asyncio").run(service.handle(issued.token, "lazymc", request))
    replay = __import__("asyncio").run(service.handle(issued.token, "lazymc", request))
    assert first.body == replay.body == {"state": "ready", "readiness_generation": 4}
    assert [call.action.kind for call in calls] == ["get_status", "start"]
    conflict = service.parse_request({"request_id": request_id, "action": {"kind": "status"}})
    conflict_response = __import__("asyncio").run(service.handle(issued.token, "lazymc", conflict))
    assert conflict_response.status == 409
    assert "confirm" not in repr(calls[1].action).casefold()


@pytest.mark.parametrize("state", [ObservedState.STARTING, ObservedState.RUNNING])
@pytest.mark.parametrize("wire_dict", [False, True])
def test_wake_treats_slot_conflict_as_idempotent_only_when_target_is_active_owner(state, wire_dict):
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        if isinstance(request.action, WaitReadiness):
            result = ReadinessResult(
                profile_id=CAPABILITY_START_PROFILE,
                generation=12,
                outcome="success",
            )
            return RpcSuccess(
                request_id=request.request_id,
                result=result.model_dump(mode="json") if wire_dict else result,
            )
        snapshot = _snapshot()
        result = snapshot.model_copy(
            update={"profiles": (snapshot.profiles[0].model_copy(update={"state": state}),)}
        )
        return RpcSuccess(
            request_id=request.request_id,
            result=result.model_dump(mode="json") if wire_dict else result,
        )

    service = CapabilityService(store, rpc)
    request = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    response = asyncio.run(service.handle(issued.token, "lazymc", request))
    replay = asyncio.run(service.handle(issued.token, "lazymc", request))

    assert response.status == 200
    expected = {"state": "ready", "already_active": True}
    if state is ObservedState.STARTING:
        expected["readiness_generation"] = 12
    assert response.body == replay.body == expected
    assert [call.action.kind for call in calls] == (
        ["get_status", "wait_readiness"] if state is ObservedState.STARTING else ["get_status"]
    )
    assert all(call.actor == "capability-waker" for call in calls)
    assert calls[0].action.refresh is True


@pytest.mark.parametrize(
    ("slot_owner", "state"),
    [
        (ProfileId.TERRARIA_VANILLA, ObservedState.RUNNING),
        (CAPABILITY_START_PROFILE, ObservedState.STOPPING),
        (CAPABILITY_START_PROFILE, ObservedState.STOPPED),
        (CAPABILITY_START_PROFILE, ObservedState.FAILED),
        (CAPABILITY_START_PROFILE, ObservedState.BLOCKED),
    ],
)
def test_wake_preserves_slot_conflict_unless_target_is_active_owner(slot_owner, state):
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        snapshot = _snapshot()
        return RpcSuccess(
            request_id=request.request_id,
            result=snapshot.model_copy(
                update={
                    "profiles": (
                        snapshot.profiles[0].model_copy(update={"slot_owner": slot_owner, "state": state}),
                    )
                }
            ),
        )

    service = CapabilityService(store, rpc)
    request = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    response = asyncio.run(service.handle(issued.token, "lazymc", request))

    assert response.status == 409
    assert response.body["error"]["code"] == "slot_conflict"
    assert [call.action.kind for call in calls] == ["get_status"]


def test_wake_rechecks_same_active_owner_after_start_race_conflict():
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        if isinstance(request.action, GetStatus):
            result = _stopped_snapshot() if len(calls) == 1 else _snapshot()
            return RpcSuccess(request_id=request.request_id, result=result)
        return RpcFailure(
            request_id=request.request_id,
            error=RpcError(code=ErrorCode.SLOT_CONFLICT, message="slot is reserved", retryable=False),
        )

    service = CapabilityService(store, rpc)
    request = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    response = asyncio.run(service.handle(issued.token, "lazymc", request))

    assert response.status == 200
    assert response.body == {"state": "ready", "already_active": True}
    assert [call.action.kind for call in calls] == ["get_status", "start", "get_status"]
    assert len({call.request_id for call in calls}) == 3


@pytest.mark.parametrize(
    ("status_mode", "expected_status", "expected_code"),
    [
        ("missing", 503, "upstream_unavailable"),
        ("untyped", 503, "upstream_unavailable"),
        ("failure", 503, "upstream_unavailable"),
        ("transport", 503, "upstream_unavailable"),
    ],
)
def test_wake_slot_conflict_fails_closed_when_status_does_not_prove_owner(
    status_mode, expected_status, expected_code
):
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        if status_mode == "missing":
            return RpcSuccess(request_id=request.request_id, result=_snapshot().model_copy(update={"profiles": ()}))
        if status_mode == "untyped":
            return RpcSuccess(request_id=request.request_id, result={"profiles": []})
        if status_mode == "failure":
            return RpcFailure(
                request_id=request.request_id,
                error=RpcError(code=ErrorCode.UPSTREAM_UNAVAILABLE, message="status unavailable", retryable=True),
            )
        raise OSError("status transport unavailable")

    service = CapabilityService(store, rpc)
    request = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    response = asyncio.run(service.handle(issued.token, "lazymc", request))

    assert response.status == expected_status
    assert response.body["error"]["code"] == expected_code
    assert "already_active" not in response.body
    assert [call.action.kind for call in calls] == ["get_status"]


def test_revocation_and_rate_budget_fail_closed():
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE, rate_budget=1)

    async def rpc(request):
        if isinstance(request.action, GetStatus):
            return RpcSuccess(request_id=request.request_id, result=_stopped_snapshot())
        return RpcSuccess(request_id=request.request_id, result={
            "job_id": "wake", "state": "running",
            "readiness_generation": 20, "readiness": "success",
        })

    service = CapabilityService(store, rpc)
    first = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    second = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    assert __import__("asyncio").run(service.handle(issued.token, "lazymc", first)).status == 200
    assert __import__("asyncio").run(service.handle(issued.token, "lazymc", second)).status == 429
    store.revoke(issued.token_id)
    revoked = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert __import__("asyncio").run(service.handle(issued.token, "lazymc", revoked)).status == 401


def test_status_rate_window_rolls_over_and_idempotent_replay_does_not_consume_budget(tmp_path):
    clock = _Clock()
    database_path = tmp_path / "capabilities.sqlite"
    store = CapabilityTokenStore(sqlite3.connect(database_path), clock=clock)
    issued = store.issue(role="observer", audience="helios-mcp", rate_budget=1)

    async def rpc(request):
        if isinstance(request.action, GetStatus):
            return RpcSuccess(request_id=request.request_id, result=_stopped_snapshot())
        return RpcSuccess(request_id=request.request_id, result={"state": "ok"})

    service = CapabilityService(store, rpc)
    first = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", first)).status == 200
    store.db.close()
    store = CapabilityTokenStore(sqlite3.connect(database_path), clock=clock)
    service = CapabilityService(store, rpc)
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", first)).status == 200
    exhausted = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", exhausted)).status == 429

    clock.advance(seconds=61)
    rolled = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", rolled)).status == 200


def test_wake_cooldown_is_separate_from_status_window_and_rolls_over():
    clock = _Clock()
    store = _store(clock)
    issued = store.issue(
        role="waker",
        audience="helios-mcp",
        profile_id=CAPABILITY_START_PROFILE,
        rate_budget=1,
        wake_cooldown=timedelta(seconds=30),
    )

    async def rpc(request):
        if isinstance(request.action, GetStatus):
            return RpcSuccess(request_id=request.request_id, result=_stopped_snapshot())
        return RpcSuccess(request_id=request.request_id, result={
            "job_id": "wake", "state": "running",
            "readiness_generation": 21, "readiness": "success",
        })

    service = CapabilityService(store, rpc)
    wake = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    status = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    second_wake = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", wake)).status == 200
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", status)).status == 200
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", second_wake)).status == 429

    clock.advance(seconds=31)
    third_wake = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "wake"}})
    assert __import__("asyncio").run(service.handle(issued.token, "helios-mcp", third_wake)).status == 200


def test_token_only_http_routes_do_not_accept_operator_session_or_raw_profile():
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)

    async def rpc(request):
        snapshot = _snapshot()
        other = snapshot.profiles[0].model_copy(
            update={
                "profile_id": ProfileId.TERRARIA_VANILLA,
                "state": ObservedState.BLOCKED,
                "health": HealthState.UNKNOWN,
                "pid": None,
                "started_at": None,
                "uptime_seconds": None,
                "players_online": None,
                "required_ports_ready": False,
            }
        )
        return RpcSuccess(
            request_id=request.request_id,
            result=snapshot.model_copy(update={"profiles": (snapshot.profiles[0], other)}).model_dump(mode="json"),
        )

    app = create_app(
        capability_service=CapabilityService(store, rpc),
        proxy_credential="secret",
        session_db=store.db,
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {issued.token}",
        "X-Horizon-Capability-Audience": "lazymc",
    }
    response = client.post(
        "/api/v1/capability/status",
        headers=headers,
        json={"request_id": str(uuid4()), "action": {"kind": "status"}},
    )
    assert response.status_code == 200
    assert response.json()["profiles"][0]["profile_id"] == CAPABILITY_START_PROFILE.value
    assert len(response.json()["profiles"]) == 1
    no_token = client.post("/api/v1/capability/status", json={"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert no_token.status_code == 401


def test_waker_status_rejects_malformed_wire_dict_instead_of_returning_it():
    store = _store()
    issued = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)

    async def rpc(request):
        return RpcSuccess(request_id=request.request_id, result={"profiles": [{"unexpected": "field"}]})

    app = create_app(
        capability_service=CapabilityService(store, rpc),
        proxy_credential="secret",
        session_db=store.db,
    )
    response = TestClient(app).post(
        "/api/v1/capability/status",
        headers={
            "Authorization": f"Bearer {issued.token}",
            "X-Horizon-Capability-Audience": "lazymc",
        },
        json={"request_id": str(uuid4()), "action": {"kind": "status"}},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"
    assert "profiles" not in response.json()


def test_mcp_waker_exposes_fixed_profile_horizon_wake():
    store = _store()
    issued = store.issue(role="waker", audience="helios-mcp", profile_id=CAPABILITY_START_PROFILE)
    calls = []

    async def rpc(request):
        calls.append(request)
        if isinstance(request.action, GetStatus):
            return RpcSuccess(request_id=request.request_id, result=_stopped_snapshot())
        return RpcSuccess(request_id=request.request_id, result={
            "job_id": "mcp-wake", "state": "running",
            "readiness_generation": 8, "readiness": "success",
        })

    app = create_app(
        capability_service=CapabilityService(store, rpc),
        proxy_credential="secret",
        session_db=store.db,
    )
    response = TestClient(app).post(
        "/api/v1/capability/wake",
        headers={
            "Authorization": f"Bearer {issued.token}",
            "X-Horizon-Capability-Audience": "helios-mcp",
        },
        json={"request_id": str(uuid4()), "action": {"kind": "wake"}},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "ready", "readiness_generation": 8}
    assert [call.action.kind for call in calls] == ["get_status", "start"]
    assert calls[1].action.profile_id is CAPABILITY_START_PROFILE


def _capability_endpoint(app):
    return next(route.endpoint for route in app.routes if route.path == "/api/v1/capability/status")


def _stream_request(chunks, *, content_length=None):
    remaining = list(chunks)
    headers = [
        (b"authorization", b"Bearer hc_oversized-test"),
        (b"x-horizon-capability-audience", b"lazymc"),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))

    async def receive():
        if remaining:
            body = remaining.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(remaining)}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/capability/status",
            "raw_path": b"/api/v1/capability/status",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive=receive,
    )


@pytest.mark.parametrize("content_length", [None, "0"])
def test_capability_body_limit_rejects_chunked_or_false_length_before_json_parse(content_length):
    store = _store()

    async def rpc(_request):
        raise AssertionError("oversized capability body must not reach RPC")

    app = create_app(
        capability_service=CapabilityService(store, rpc),
        proxy_credential="secret",
        session_db=store.db,
    )
    oversized = b'{"request_id":"' + str(uuid4()).encode() + b'","action":{"kind":"status","window":"6h","padding":"' + b"a" * 5000 + b'"}}'
    response = asyncio.run(
        _capability_endpoint(app)(
            _stream_request([oversized[:2048], oversized[2048:]], content_length=content_length)
        )
    )
    assert response.status_code == 413
