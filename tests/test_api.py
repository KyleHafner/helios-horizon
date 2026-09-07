from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.api import ROUTE_ACTIONS
from game_control.protocol import GetStatus, GetBenchmarks, ExportBenchmarks, BenchmarkExport, BenchmarkOverview, BenchmarkPresetView, JobAccepted, RpcRequest, RpcSuccess, StatusSnapshot
from game_control.protocol import ErrorCode, GetLogs, RpcError, RpcFailure
from starlette.requests import Request

from game_control.web_main import create_app
from game_control.web_main import AdaptiveStatusCadence
from game_control.auth import SESSION_COOKIE, SessionStore


def test_invalid_session_cookie_is_cleared_for_trusted_bootstrap():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    app = create_app(proxy_credential="secret", session_db=db)
    trusted = {
        "X-Game-Control-Proxy": "secret",
        "X-authentik-username": "operator",
    }
    with TestClient(app, base_url="https://games.example.com") as client:
        first = client.get("/api/v1/session", headers=trusted)
        old_token = client.cookies.get(SESSION_COOKIE)
        assert first.status_code == 200 and old_token
        SessionStore(db).revoke(old_token)

        rejected = client.get("/api/v1/session", headers=trusted)
        assert rejected.status_code == 401
        assert "Max-Age=0" in rejected.headers["set-cookie"]

        refreshed = client.get("/api/v1/session", headers=trusted)
        assert refreshed.status_code == 200
        assert client.cookies.get(SESSION_COOKIE) != old_token

        untrusted = client.get(
            "/api/v1/session",
            headers={"X-Game-Control-Proxy": "wrong", "X-authentik-username": "operator"},
        )
        assert untrusted.status_code == 403


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
    commands = client.get("/commands.js")
    palette = client.get("/palette.js")
    stylesheet = client.get("/styles.css")

    assert index.status_code == 200
    assert '<main id="main-content"' in index.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "const PROFILE_FALLBACK" in script.text
    assert commands.status_code == 200
    assert commands.headers["content-type"].startswith("text/javascript")
    assert "window.HORIZON_COMMANDS" in commands.text
    assert palette.status_code == 200
    assert palette.headers["content-type"].startswith("text/javascript")
    assert "window.HORIZON_PALETTE" in palette.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert ".active-slot" in stylesheet.text
    assert 'id="conn-state"' in index.text
    assert 'id="tab-benchmarks"' in index.text
    assert "function renderBenchmarks" in script.text
    assert ".benchmark-verdict" in stylesheet.text


def test_installed_web_assets_are_world_readable_for_the_unprivileged_web_service(tmp_path):
    from ops.install import Installer

    installer = Installer(tmp_path)
    web = tmp_path / "opt/game-control/web"
    directory = next(item for item in installer.directories() if item[0] == web)
    assets = {
        path.name: mode
        for path, (_source, mode) in installer.expected_files().items()
        if path.parent == web
    }

    assert directory[1:] == (0o755, "root", "root")
    assert assets == {
        "app.js": 0o644,
        "commands.js": 0o644,
        "index.html": 0o644,
        "palette.js": 0o644,
        "styles.css": 0o644,
    }


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


def _backup_client(rpc):
    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    return client, {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}


def _backup_headers(client, auth, **extra):
    csrf = client.get("/api/v1/session", headers=auth).json()["csrf_token"]
    return {**auth, "X-CSRF-Token": csrf, "Origin": "https://games.example.com", **extra}


def test_backup_idempotency_key_replays_without_duplicate_controller_execution():
    calls: list[RpcRequest] = []
    completed: dict[str, tuple[object, RpcSuccess]] = {}

    async def rpc(request: RpcRequest):
        calls.append(request)
        key = str(request.request_id)
        if key in completed:
            original_action, response = completed[key]
            if request.action != original_action:
                return RpcFailure(
                    request_id=request.request_id,
                    error=RpcError(code=ErrorCode.REQUEST_ID_CONFLICT, message="request id was already used", retryable=False),
                )
            return response
        response = RpcSuccess(request_id=request.request_id, result=JobAccepted(job_id="backup-job", state="running"))
        completed[key] = (request.action, response)
        return response

    client, auth = _backup_client(rpc)
    key = str(uuid4())
    first = client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": key}), json={"protected": True})
    replay = client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": key}), json={"protected": True})
    conflict = client.post("/api/v1/profiles/terraria-tmod/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": key}), json={"protected": True})

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert [str(call.request_id) for call in calls] == [key, key, key]
    assert calls[0].action == calls[1].action
    assert calls[0].action != calls[2].action


def test_backup_idempotency_key_retry_after_timeout_reuses_request_id():
    calls: list[RpcRequest] = []
    completed: dict[str, RpcSuccess] = {}

    async def rpc(request: RpcRequest):
        calls.append(request)
        key = str(request.request_id)
        if key in completed:
            return completed[key]
        completed[key] = RpcSuccess(request_id=request.request_id, result=JobAccepted(job_id="backup-job", state="running"))
        raise TimeoutError("response lost after controller execution")

    client, auth = _backup_client(rpc)
    key = str(uuid4())
    first = client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": key}), json={})
    retry = client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": key}), json={})

    assert first.status_code == 503
    assert retry.status_code == 200
    assert len(calls) == 2
    assert calls[0].request_id == calls[1].request_id


@pytest.mark.parametrize("value", [" ", "not-a-uuid", "0" * 129, str(uuid4()).upper()])
def test_backup_idempotency_key_rejects_malformed_values_before_rpc(value):
    calls = []

    async def rpc(request: RpcRequest):
        calls.append(request)
        return RpcSuccess(request_id=request.request_id, result={"ok": True})

    client, auth = _backup_client(rpc)
    response = client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth, **{"Idempotency-Key": value}), json={})

    assert response.status_code == 422
    assert not calls


def test_backup_idempotency_key_rejects_multiple_values_before_rpc():
    calls = []

    async def rpc(request: RpcRequest):
        calls.append(request)
        return RpcSuccess(request_id=request.request_id, result={"ok": True})

    client, auth = _backup_client(rpc)
    response = client.request(
        "POST",
        "/api/v1/profiles/minecraft/backups",
        headers=[*_backup_headers(client, auth).items(), ("Idempotency-Key", str(uuid4())), ("Idempotency-Key", str(uuid4()))],
        json={},
    )

    assert response.status_code == 422
    assert not calls


def test_backup_without_idempotency_key_preserves_generated_request_ids():
    calls: list[RpcRequest] = []

    async def rpc(request: RpcRequest):
        calls.append(request)
        return RpcSuccess(request_id=request.request_id, result={"ok": True})

    client, auth = _backup_client(rpc)
    for _ in range(2):
        assert client.post("/api/v1/profiles/minecraft/backups", headers=_backup_headers(client, auth), json={}).status_code == 200

    assert len(calls) == 2
    assert calls[0].request_id != calls[1].request_id


def test_benchmark_csv_export_has_attachment_type_and_formula_safe_cells():
    async def rpc(actor, action):
        if isinstance(action, ExportBenchmarks):
            return RpcSuccess(request_id=uuid4(), result=BenchmarkExport(format="csv", content="id\nrun\n"))
        if isinstance(action, GetBenchmarks):
            from game_control.models import ProfileId
            from game_control.protocol import BenchmarkRunSummary
            run = BenchmarkRunSummary(
                    id="run", profile_id=ProfileId.MINECRAFT, baseline_preset="current",
                candidate_preset="candidate", state="succeeded",
                created_at="2026-08-23T00:00:00Z", finished_at="2026-08-23T00:01:00Z",
                overall_verdict="better",
            )
            return RpcSuccess(request_id=uuid4(), result=BenchmarkOverview(profile_id=action.profile_id, available=True, presets=(BenchmarkPresetView(id="current", label="Current"),), runs=(run,)))
        return RpcSuccess(request_id=uuid4(), result=StatusSnapshot(generation=0, observed_at="2026-01-01T00:00:00Z", profiles=()))

    app = create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:")
    headers = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
    with TestClient(app, base_url="https://games.example.com") as client:
        session = client.get("/api/v1/session", headers=headers)
        csrf = session.json()["csrf_token"]
        response = client.get("/api/v1/benchmarks/minecraft-sunlit-cobblemon/export?format=csv", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "run" in response.text
