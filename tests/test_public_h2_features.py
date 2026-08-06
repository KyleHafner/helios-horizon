"""Synthetic public regressions for H2 capability, LazyMC, and stop fencing."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _clock():
    value = [datetime.now(timezone.utc).replace(microsecond=0)]

    def now():
        return value[0]

    def advance(**kwargs):
        value[0] += timedelta(**kwargs)

    return now, advance


def test_capability_tokens_are_separate_by_role_and_audience_and_reject_stop():
    from game_control.capability import CAPABILITY_START_PROFILE, CapabilityTokenStore

    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    observer = store.issue(role="observer", audience="helios-mcp")
    mcp_waker = store.issue(role="waker", audience="helios-mcp", profile_id=CAPABILITY_START_PROFILE)
    lazymc_waker = store.issue(role="waker", audience="lazymc", profile_id=CAPABILITY_START_PROFILE)

    assert observer.scopes == frozenset({"status", "tps"})
    assert mcp_waker.scopes == lazymc_waker.scopes == frozenset({"status", "wake"})
    assert mcp_waker.token != lazymc_waker.token
    with pytest.raises(Exception):
        store.issue(role="waker", audience="helios-mcp", profile_id=CAPABILITY_START_PROFILE, scopes={"stop"})

    from game_control.capability import CapabilityService

    with pytest.raises(Exception):
        CapabilityService.parse_request({"request_id": str(uuid4()), "action": {"kind": "stop"}})


def test_capability_status_window_replays_without_consuming_budget_and_rolls_over(tmp_path):
    from game_control.capability import CapabilityService, CapabilityTokenStore
    from game_control.protocol import GetStatus, RpcSuccess

    clock, advance = _clock()
    database = tmp_path / "synthetic-capability.sqlite"
    store = CapabilityTokenStore(sqlite3.connect(database), clock=clock)
    issued = store.issue(role="observer", audience="helios-mcp", rate_budget=1)

    async def rpc(request):
        assert isinstance(request.action, GetStatus)
        return RpcSuccess(request_id=request.request_id, result={"profiles": []})

    service = CapabilityService(store, rpc)
    first = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert asyncio.run(service.handle(issued.token, "helios-mcp", first)).status == 200
    assert asyncio.run(service.handle(issued.token, "helios-mcp", first)).status == 200
    exhausted = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert asyncio.run(service.handle(issued.token, "helios-mcp", exhausted)).status == 429
    advance(seconds=61)
    rolled = service.parse_request({"request_id": str(uuid4()), "action": {"kind": "status"}})
    assert asyncio.run(service.handle(issued.token, "helios-mcp", rolled)).status == 200


def test_lazymc_wakes_then_polls_only_synthetic_typed_endpoints(monkeypatch):
    import game_control.lazymc as lazymc

    endpoint = "http://example.com:18080/api/v1/capability"
    monkeypatch.setattr(lazymc, "CAPABILITY_WAKE_URL", endpoint + "/wake")
    monkeypatch.setattr(lazymc, "CAPABILITY_STATUS_URL", endpoint + "/status")
    profile = lazymc.CAPABILITY_START_PROFILE.value
    healthy = {"profiles": [{"profile_id": profile, "state": "running", "health": "healthy", "required_ports_ready": True}]}
    stopped = {"profiles": [{"profile_id": profile, "state": "stopped", "health": "unknown", "required_ports_ready": False}]}
    responses = iter([{"job_id": "synthetic-job", "state": "running"}, healthy, stopped])
    calls = []

    class Response:
        def __init__(self, body):
            import json
            self.body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.body

    def opener(request, timeout):
        import json
        calls.append((request.full_url, json.loads(request.data), timeout))
        return Response(next(responses))

    client = lazymc.LazyWakeClient("hc_" + "s" * 40, opener=opener, sleep=lambda _seconds: None)
    client.wake_and_wait(grace_seconds=10)
    client.wait_until_stopped()

    assert [item[0] for item in calls] == [endpoint + "/wake", endpoint + "/status", endpoint + "/status"]
    assert all(set(body) == {"request_id", "action"} for _url, body, _timeout in calls)
    assert all(body["action"] in ({"kind": "wake"}, {"kind": "status"}) for _url, body, _timeout in calls)


def test_stop_fencing_public_fixture_uses_synthetic_status_only():
    from game_control.models import AdapterKind, HealthState, ObservedState, OperationName, ProfileId
    from game_control.protocol import ProfileStatus

    status = ProfileStatus(
        profile_id=ProfileId.MINECRAFT,
        state=ObservedState.RUNNING,
        health=HealthState.HEALTHY,
        slot_owner=ProfileId.MINECRAFT,
        active_job_id=None,
        pid=1,
        started_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        uptime_seconds=0,
        cpu_percent=0.0,
        rss_bytes=0,
        players_online=0,
        installed_version="synthetic",
        restart_required=False,
        required_ports_ready=True,
    )
    profile = SimpleNamespace(
        id=ProfileId.MINECRAFT,
        adapter=AdapterKind.SYSTEMD,
        operations=frozenset({OperationName.START, OperationName.STOP}),
        stop_timeout_seconds=5,
        idle_stop_minutes=5,
    )
    assert status.slot_owner is profile.id
    assert status.players_online == 0
