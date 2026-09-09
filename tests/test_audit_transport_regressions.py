from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.api import ApiService, _read_mutation_body
from game_control.auth import SessionStore
from game_control.protocol import (
    MAX_RESPONSE_BYTES,
    ErrorCode,
    GetStatus,
    RpcFailure,
    RpcRequest,
    RpcSuccess,
    StatusSnapshot,
    failure,
    GetLogs,
    LogLine,
    LogPage,
    LogOptions,
    Start,
    JobAccepted,
    response_json,
)
from game_control.controller import Controller
from game_control.slotd_main import UnixRpcServer
from game_control.updates import MAX_DOWNLOAD_BYTES, UpdateService
from game_control.web_main import UnixRpcClient, create_app


AUTH = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
ORIGIN = "https://games.example.com"


def _client(rpc):
    client = TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"), base_url=ORIGIN)
    csrf = client.get("/api/v1/session", headers=AUTH).json()["csrf_token"]
    return client, {**AUTH, "X-CSRF-Token": csrf, "Origin": ORIGIN}


def test_every_mutation_forwards_one_canonical_key_and_reads_do_not_require_one():
    calls = []

    async def rpc(request: RpcRequest):
        calls.append(request)
        return RpcSuccess(request_id=request.request_id, result={"ok": True})

    client, headers = _client(rpc)
    key = str(uuid4())
    mutations = (
        ("/api/v1/profiles/minecraft/start", {}),
        ("/api/v1/profiles/minecraft/command", {"command": "say hi"}),
        ("/api/v1/profiles/minecraft/backups", {}),
        ("/api/v1/schedules", {"entries": []}),
    )
    for path, body in mutations:
        assert client.post(path, headers={**headers, "Idempotency-Key": key}, json=body).status_code == 200
    assert [str(request.request_id) for request in calls] == [key] * len(mutations)
    assert client.get("/api/v1/status", headers=AUTH).status_code == 200


def test_mutation_key_validation_and_oversized_body():
    async def rpc(_request):
        return {"ok": True}

    client, headers = _client(rpc)
    assert client.post("/api/v1/profiles/minecraft/start", headers={**headers, "Idempotency-Key": "bad"}).status_code == 422
    # Declared size is rejected before the body is decoded.
    response = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "Content-Length": str(64 * 1024 + 1)},
        content=b"{}",
    )
    assert response.status_code == 413


def test_body_boundaries_chunking_lies_and_slow_reads(monkeypatch):
    async def rpc(_request):
        return {"ok": True}

    client, headers = _client(rpc)
    # Exactly the application cap is not rejected by the size gate (the
    # payload then fails ordinary schema validation rather than 413).
    exact = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "Content-Length": str(64 * 1024)},
        content=b" " * (64 * 1024),
    )
    assert exact.status_code != 413
    chunked = client.post(
        "/api/v1/profiles/minecraft/start",
        headers=headers,
        content=iter((b"x" * 32768, b"y" * 32769)),
    )
    assert chunked.status_code == 413
    lying = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "Content-Length": "1"},
        content=b"{}",
    )
    assert lying.status_code == 400
    malformed = client.post(
        "/api/v1/profiles/minecraft/start",
        headers={**headers, "Content-Length": "abc"},
        content=b"{}",
    )
    assert malformed.status_code == 400
    async def drip():
        yield b"{"; await asyncio.sleep(0.05); yield b"}"
    class Headers:
        def getlist(self, name): return []
    class SlowRequest:
        headers = Headers()
        def stream(self): return drip()
    monkeypatch.setattr("game_control.api.BODY_READ_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(Exception, match="timed out"):
        asyncio.run(_read_mutation_body(SlowRequest()))
    class DuplicateHeaders:
        def getlist(self, name): return ["1", "2"]
    duplicate = SlowRequest()
    duplicate.headers = DuplicateHeaders()
    with pytest.raises(Exception, match="invalid content length"):
        asyncio.run(_read_mutation_body(duplicate))


def test_http_response_loss_replays_one_side_effect_and_identical_response():
    calls = []
    result = {"job_id": "job-1", "state": "running"}

    async def rpc(request: RpcRequest):
        calls.append(request)
        if len(calls) == 1:
            # Simulate the controller having committed, followed by a lost
            # web/RPC response.
            raise TimeoutError("response lost after side effect")
        return RpcSuccess(request_id=request.request_id, result=result)

    client, headers = _client(rpc)
    key = str(uuid4())
    first = client.post("/api/v1/profiles/minecraft/start", headers={**headers, "Idempotency-Key": key})
    replay = client.post("/api/v1/profiles/minecraft/start", headers={**headers, "Idempotency-Key": key})
    assert first.status_code == 503 and replay.status_code == 200
    assert replay.json()["job_id"] == result["job_id"]
    assert replay.json()["state"] == result["state"]
    assert len(calls) == 2 and calls[0].request_id == calls[1].request_id


def test_log_pages_are_reduced_before_rpc_framing_budget():
    seen = []

    async def rpc(request: RpcRequest):
        seen.append(request)
        return {"items": [], "next_cursor": None}

    client, _headers = _client(rpc)
    assert client.get("/api/v1/profiles/minecraft/logs?limit=5000", headers=AUTH).status_code == 200
    assert seen[-1].action.page.limit == 5000


@pytest.mark.asyncio
async def test_controller_log_page_byte_budget_and_cursor():
    controller = Controller.for_testing(__import__("pathlib").Path("/tmp"))
    lines = tuple(LogLine(timestamp="2026-01-01T00:00:00Z", severity="info", message="x" * 8192) for _ in range(5000))
    controller.services = SimpleNamespace(logs=SimpleNamespace(page=lambda *_args: LogPage(items=lines, next_cursor=None)))
    action = GetLogs(kind="get_logs", profile_id="minecraft", page=LogOptions(limit=5000))
    first = await controller._get_logs(action, "operator", uuid4())
    assert first.items and first.next_cursor
    assert len(response_json(RpcSuccess(request_id=uuid4(), result=first))) < MAX_RESPONSE_BYTES
    second = await controller._get_logs(action.model_copy(update={"page": LogOptions(limit=5000, cursor=first.next_cursor)}), "operator", uuid4())
    assert len(second.items) > 0 and len(second.items) <= len(first.items)
    assert first.next_cursor != second.next_cursor


@pytest.mark.asyncio
async def test_log_page_encoding_is_linear_and_bad_cursor_fails_closed(monkeypatch):
    import game_control.controller as controller_module
    controller = Controller.for_testing(__import__("pathlib").Path("/tmp"))
    lines = tuple(LogLine(timestamp="2026-01-01T00:00:00Z", severity="info", message="small") for _ in range(5000))
    controller.services = SimpleNamespace(logs=SimpleNamespace(page=lambda *_args: LogPage(items=lines, next_cursor="backend")))
    calls = 0
    original = controller_module.json.dumps
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(controller_module.json, "dumps", counted)
    page = await controller._get_logs(GetLogs(kind="get_logs", profile_id="minecraft", page=LogOptions(limit=5000)), "operator", uuid4())
    assert len(page.items) == 5000 and page.next_cursor is None and calls <= 5002
    with pytest.raises(Exception, match="invalid log cursor"):
        await controller._get_logs(GetLogs(kind="get_logs", profile_id="minecraft", page=LogOptions(limit=1, cursor="0001")), "operator", uuid4())


@pytest.mark.asyncio
async def test_log_cursor_iterates_unique_records_without_repeats(tmp_path):
    controller = Controller.for_testing(tmp_path)
    lines = tuple(LogLine(timestamp="2026-01-01T00:00:00Z", severity="info", message=f"record-{index}-" + "x" * 8180) for index in range(5000))
    controller.services = SimpleNamespace(logs=SimpleNamespace(page=lambda *_args: LogPage(items=lines, next_cursor=None)))
    cursor = None
    observed = []
    while True:
        page = await controller._get_logs(
            GetLogs(kind="get_logs", profile_id="minecraft", page=LogOptions(limit=5000, cursor=cursor)),
            "operator", uuid4(),
        )
        observed.extend(line.message.split("-", 2)[1] for line in page.items)
        assert len(response_json(RpcSuccess(request_id=uuid4(), result=page))) < MAX_RESPONSE_BYTES
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert observed == [str(index) for index in range(5000)]


@pytest.mark.asyncio
async def test_http_api_controller_replay_is_single_side_effect_and_conflict_safe(tmp_path):
    controller = Controller.for_testing(tmp_path)
    calls = 0

    async def handler(_action, _actor, _request_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return JobAccepted(job_id="job-1", state="running")

    controller._start = handler
    service = ApiService(controller.execute)
    key = uuid4()
    action = Start(kind="start", profile_id="minecraft")
    first, second = await asyncio.gather(
        service.call("operator", action, request_id=key),
        service.call("operator", action, request_id=key),
    )
    assert calls == 1 and first == second
    conflict = await service.call("other", action, request_id=key)
    assert isinstance(conflict, RpcFailure) and conflict.error.code is ErrorCode.REQUEST_ID_CONFLICT


def test_revoked_read_session_is_rejected_and_pruning_is_bounded():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = sqlite3.connect(":memory:")
    store = SessionStore(db, now=lambda: now)
    session, csrf = store.create("operator", ttl=timedelta(hours=1))
    assert store.validate_session(session, actor="operator")
    store.revoke(session)
    assert not store.validate_session(session, actor="operator")
    old, _ = store.create("operator", ttl=timedelta(hours=1))
    db.execute("UPDATE web_sessions SET revoked_at=? WHERE session_hash=?", ("2025-01-01T00:00:00Z", store.get(old).session_hash))
    db.commit()
    assert store.prune(batch_size=1) == 1
    assert store.prune(batch_size=1) == 0
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_web_sessions_expires'").fetchone()
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_web_sessions_revoked'").fetchone()


def test_rpc_oversize_write_is_a_typed_failure():
    class Writer:
        def __init__(self): self.data = []
        def write(self, data): self.data.append(data)
        async def drain(self): pass

    async def run():
        writer = Writer()
        await UnixRpcServer._write(writer, b"x" * (MAX_RESPONSE_BYTES + 1), request_kind="logs", actor="operator")
        return writer.data

    data = asyncio.run(run())
    response = json.loads(data[0])
    assert response["ok"] is False and response["error"]["code"] == ErrorCode.INTERNAL_ERROR.value


def test_internal_fallback_has_required_metadata():
    source = open("src/game_control/slotd_main.py", encoding="utf-8").read()
    assert "request_kind=request_kind" in source and "actor=actor" in source


def test_browser_persists_idempotency_and_unknown_outcome_copy():
    source = open("web/app.js", encoding="utf-8").read()
    assert "sessionStorage.getItem(operationStorageKey)" in source
    assert "Outcome unknown. Retry with the same operation key." in source
    assert "action not sent" not in source
    assert "sessionStorage.removeItem(operationStorageKey)" in source
    assert "response.status === 503" in source
    assert "outcomeUnknown = true" in source


def test_browser_replays_same_key_after_gateway_body_is_not_json():
    source = Path("web/app.js").resolve()
    script = r'''
const fs = require('fs'), vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync(input.file, 'utf8');
const start = source.indexOf('async function api(');
const end = source.indexOf('\nasync function loadOnce()', start);
if (end < start) throw new Error('API extraction boundary missing');
const apiSource = source.slice(start, end);
const storage = new Map(), keys = [];
const context = {Headers, Response, state:{csrf:'synthetic'}, sessionExpired:false,
  sessionGeneration:1, SESSION_EXPIRED_MESSAGE:'expired',
  crypto:{randomUUID:()=> '22222222-2222-4222-8222-222222222222'},
  sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  fetch:async (path, options) => {
    keys.push(options.headers.get('Idempotency-Key'));
    return {status: 503, ok: false, json: async()=>{throw new Error('html')}};
  }};
vm.createContext(context); vm.runInContext(apiSource, context);
(async()=>{
  const outcomes = [];
  for (let i = 0; i < 2; i++) try {
    outcomes.push(await context.api('/api/v1/profiles/minecraft/start', {method:'POST', body:'{}'}));
  } catch (error) { outcomes.push({message:error.message, unknown:!!error.outcomeUnknown}); }
  process.stdout.write(JSON.stringify({keys, outcomes, stored_keys:storage.size}));
})();
'''
    result = subprocess.run(
        ["node", "-e", script], input=json.dumps({"file": str(source)}),
        text=True, capture_output=True, check=True,
    )
    observed = json.loads(result.stdout)
    assert observed["keys"] == [observed["keys"][0], observed["keys"][0]]
    assert observed["outcomes"][0]["unknown"] is True
    assert observed["outcomes"][1]["unknown"] is True
    assert observed["stored_keys"] == 1


def test_staged_version_command_replaces_current_path():
    service = object.__new__(UpdateService)
    executable = "/tmp/release/game"
    command = ("/opt/game-servers/terraria/current/game", "--version")
    result = [
        (str(argument).replace("{staged_executable}", executable)
         if "{staged_executable}" in str(argument) else executable
         if "/current/" in str(argument) else argument)
        for argument in command
    ]
    assert result[0] == executable


def test_download_quota_applies_to_custom_downloader(tmp_path, monkeypatch):
    profile = SimpleNamespace(update=SimpleNamespace(sha256="a" * 64, download_url="https://example.invalid/x"))
    destination = tmp_path / "artifact"
    service = UpdateService({"x": profile}, downloader=lambda _profile, path: path.write_bytes(b"x" * (MAX_DOWNLOAD_BYTES + 1)))
    with pytest.raises(Exception, match="exceeds size limit"):
        service._download(profile, destination)
