"""Clock-controlled retention regressions for the capability store."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


def _clock():
    value = [datetime(2026, 1, 1, tzinfo=UTC)]

    def now():
        return value[0]

    def advance(**kwargs):
        value[0] += timedelta(**kwargs)

    return now, advance


def _request(kind: str):
    from game_control.capability import CapabilityAction, CapabilityRequest

    return CapabilityRequest(request_id=uuid4(), action=CapabilityAction(kind=kind))


def _complete(store, issued, request, *, ok=True):
    audience = issued.audience.value
    grant, replay = store.begin(issued.token, audience, request)
    assert replay is None
    store.complete(grant, request, {"ok": ok}, ok=ok)


def test_successful_status_rows_are_bounded_without_a_per_token_cache(monkeypatch):
    from game_control import capability

    monkeypatch.setattr(
        capability, "CAPABILITY_STATUS_REPLAY_WINDOW", timedelta(hours=1)
    )
    monkeypatch.setattr(capability, "CAPABILITY_STATUS_REPLAY_PER_TOKEN_CAP", 4)
    monkeypatch.setattr(capability, "CAPABILITY_STATUS_REPLAY_GLOBAL_CAP", 6)
    monkeypatch.setattr(capability, "CAPABILITY_STATUS_AUDIT_PER_TOKEN_CAP", 8)
    monkeypatch.setattr(capability, "CAPABILITY_STATUS_AUDIT_GLOBAL_CAP", 12)
    monkeypatch.setattr(
        capability, "CAPABILITY_RETENTION_MAINTENANCE_INTERVAL", timedelta(seconds=1)
    )

    clock, advance = _clock()
    store = capability.CapabilityTokenStore(sqlite3.connect(":memory:"), clock=clock)
    first = store.issue(
        role="observer",
        audience="helios-mcp",
        rate_budget=1000,
        rate_window=timedelta(seconds=1),
    )
    second = store.issue(
        role="observer",
        audience="helios-mcp",
        rate_budget=1000,
        rate_window=timedelta(seconds=1),
    )

    for _ in range(12):
        _complete(store, first, _request("status"))
        advance(seconds=2)
        _complete(store, second, _request("status"))
        advance(seconds=2)

    replay_rows = store.db.execute(
        "SELECT token_id,COUNT(*) FROM capability_requests WHERE action_kind='status' AND status='completed' "
        "GROUP BY token_id"
    ).fetchall()
    audit_rows = store.db.execute(
        "SELECT token_id,COUNT(*) FROM capability_audit "
        "WHERE action='status' AND result IN ('accepted','succeeded') GROUP BY token_id"
    ).fetchall()
    assert replay_rows
    assert all(count <= 4 for _token_id, count in replay_rows)
    assert sum(count for _token_id, count in replay_rows) <= 6
    assert all(count <= 8 for _token_id, count in audit_rows)
    assert sum(count for _token_id, count in audit_rows) <= 12
    assert not any("cache" in name or "replay" in name for name in vars(store))


def test_status_replay_window_preserves_result_and_conflicts_then_expires(tmp_path):
    from game_control.capability import CapabilityService, CapabilityTokenStore
    from game_control.protocol import GetStatus, RpcSuccess

    clock, advance = _clock()
    store = CapabilityTokenStore(
        sqlite3.connect(tmp_path / "capability.sqlite"),
        clock=clock,
    )
    issued = store.issue(role="observer", audience="helios-mcp", rate_budget=10)
    calls = []

    async def rpc(request):
        assert isinstance(request.action, GetStatus)
        calls.append(request.request_id)
        return RpcSuccess(request_id=request.request_id, result={"call": len(calls)})

    service = CapabilityService(store, rpc)
    request = service.parse_request(
        {"request_id": str(uuid4()), "action": {"kind": "status"}}
    )
    first = asyncio.run(service.handle(issued.token, "helios-mcp", request))
    advance(minutes=9)
    replay = asyncio.run(service.handle(issued.token, "helios-mcp", request))
    assert replay.body == first.body
    assert len(calls) == 1

    conflict = service.parse_request(
        {
            "request_id": str(request.request_id),
            "action": {"kind": "tps", "window": "1h"},
        }
    )
    conflict_result = asyncio.run(service.handle(issued.token, "helios-mcp", conflict))
    assert conflict_result.status == 409
    assert conflict_result.body["error"]["code"] == "request_id_conflict"

    advance(minutes=2)
    stale_retry = asyncio.run(service.handle(issued.token, "helios-mcp", request))
    assert stale_retry.status == 200
    assert stale_retry.body != first.body
    assert len(calls) == 2


def test_security_and_wake_records_survive_status_pruning():
    from game_control import capability

    clock, advance = _clock()
    store = capability.CapabilityTokenStore(sqlite3.connect(":memory:"), clock=clock)
    observer = store.issue(role="observer", audience="helios-mcp", rate_budget=1000)
    failed = _request("status")
    _complete(store, observer, failed, ok=False)
    successful = _request("status")
    _complete(store, observer, successful)
    conflict = successful.model_copy(
        update={"action": capability.CapabilityAction(kind="tps", window="1h")}
    )
    with pytest.raises(capability.CapabilityError, match="request id was already used"):
        store.begin(observer.token, observer.audience, conflict)
    store.revoke(observer.token_id)

    waker = store.issue(
        role="waker",
        audience="helios-mcp",
        profile_id=capability.CAPABILITY_START_PROFILE,
        rate_budget=1000,
    )
    wake = _request("wake")
    _complete(store, waker, wake)

    advance(minutes=11)
    trigger = store.issue(role="observer", audience="helios-mcp", rate_budget=1000)
    _complete(store, trigger, _request("status"))

    audit = store.db.execute(
        "SELECT action,result FROM capability_audit WHERE token_id=? ORDER BY rowid",
        (observer.token_id,),
    ).fetchall()
    assert ("issue", "issued") in audit
    assert ("revoke", "revoked") in audit
    assert ("status", "failed") in audit
    assert ("request", "rejected") in audit
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM capability_requests WHERE token_id=? AND request_id=?",
            (observer.token_id, str(failed.request_id)),
        ).fetchone()[0]
        == 1
    )
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM capability_requests WHERE token_id=? AND request_id=?",
            (waker.token_id, str(wake.request_id)),
        ).fetchone()[0]
        == 1
    )
    assert store.db.execute(
        "SELECT action,result FROM capability_audit WHERE token_id=? AND action='wake'",
        (waker.token_id,),
    ).fetchone() == ("wake", "succeeded")
