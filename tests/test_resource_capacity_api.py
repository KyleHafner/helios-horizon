import asyncio
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from game_control.models import HealthState, ObservedState, ProfileId
from game_control.protocol import GetStatus, ProfileStatus, RpcSuccess, StatusSnapshot
from game_control import web_main
from game_control.web_main import _ResourceCapacityCache, create_app

HEADERS = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
PROFILE = ProfileId.MINECRAFT_SUNLIT_COBBLEMON
STARTED = datetime(2026, 9, 9, tzinfo=timezone.utc)


def profile(*, pid=None, state=ObservedState.RUNNING, started_at=STARTED):
    return ProfileStatus(profile_id=PROFILE, state=state, health=HealthState.HEALTHY,
        slot_owner=PROFILE if pid else None, active_job_id=None, pid=pid, started_at=started_at,
        uptime_seconds=10 if pid else None, cpu_percent=None, rss_bytes=None, players_online=0,
        installed_version="release", restart_required=False, required_ports_ready=bool(pid))


def client_for(status, monkeypatch):
    async def rpc(_actor, action):
        assert isinstance(action, GetStatus)
        return RpcSuccess(request_id=uuid4(), result=StatusSnapshot(
            generation=1, observed_at=STARTED, profiles=(status,)))
    monkeypatch.setattr(web_main, "process_start_ticks", lambda _pid: 11)
    monkeypatch.setattr(web_main, "process_started_at", lambda _pid: STARTED)
    return TestClient(create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"))


def test_resource_capacity_requires_authentication(monkeypatch):
    client = client_for(profile(pid=None, state=ObservedState.STOPPED), monkeypatch)
    response = client.get(f"/api/v1/profiles/{PROFILE}/resource-capacity")
    assert response.status_code in {401, 403}


def test_resource_capacity_stopped_profile_returns_null_pid(monkeypatch):
    client = client_for(profile(pid=None, state=ObservedState.STOPPED), monkeypatch)
    response = client.get(f"/api/v1/profiles/{PROFILE}/resource-capacity", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["pid"] is None
    assert response.json()["cpu_capacity_percent"] is None


def test_resource_capacity_pid_reuse_during_probe_is_unknown(monkeypatch):
    client = client_for(profile(pid=123), monkeypatch)
    ticks = iter((11, 12))
    monkeypatch.setattr(web_main, "process_start_ticks", lambda _pid: next(ticks))
    monkeypatch.setattr(web_main, "probe_resource_capacity", lambda _pid, _ticks: {
        "cpu_capacity_percent": 400.0, "memory_capacity_bytes": 1024,
        "cpu_source": "process_affinity", "memory_source": "cgroup_memory_max"})
    response = client.get(f"/api/v1/profiles/{PROFILE}/resource-capacity", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["cpu_source"] == "unknown"


def test_resource_capacity_start_time_mismatch_is_unknown(monkeypatch):
    client = client_for(profile(pid=123), monkeypatch)
    monkeypatch.setattr(web_main, "process_started_at", lambda _pid: STARTED.replace(second=3))
    monkeypatch.setattr(web_main, "probe_resource_capacity", lambda *_args: pytest.fail("probe must not run"))
    response = client.get(f"/api/v1/profiles/{PROFILE}/resource-capacity", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["cpu_source"] == "unknown"


def test_resource_capacity_matching_typed_status_returns_capacity(monkeypatch):
    client = client_for(profile(pid=123), monkeypatch)
    monkeypatch.setattr(web_main, "probe_resource_capacity", lambda pid, ticks: {
        "cpu_capacity_percent": 400.0, "memory_capacity_bytes": 1024,
        "cpu_source": "process_affinity", "memory_source": "cgroup_memory_max"})
    response = client.get(f"/api/v1/profiles/{PROFILE}/resource-capacity", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["cpu_capacity_percent"] == 400.0


@pytest.mark.asyncio
async def test_cache_singleflight_and_cancelled_waiter_keeps_shared_probe(monkeypatch):
    entered = threading.Event(); release = threading.Event(); calls = 0
    def probe(_pid, _ticks):
        nonlocal calls
        calls += 1
        entered.set()
        while not release.is_set():
            import time; time.sleep(0.001)
        return {"cpu_capacity_percent": 1, "memory_capacity_bytes": 2, "cpu_source": "x", "memory_source": "y"}
    monkeypatch.setattr(web_main, "probe_resource_capacity", probe)
    cache = _ResourceCapacityCache(ttl=10)
    waiter = asyncio.create_task(cache.get(7, STARTED, 11)); await asyncio.to_thread(entered.wait)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError): await waiter
    release.set()
    assert (await cache.get(7, STARTED, 11))["cpu_capacity_percent"] == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_singleflight_and_value_bounds(monkeypatch):
    monkeypatch.setattr(web_main, "probe_resource_capacity", lambda _pid, _ticks: {"cpu_capacity_percent": 1})
    cache = _ResourceCapacityCache(ttl=10)
    for pid in range(33):
        await cache.get(pid + 1, STARTED, 11)
    assert len(cache._values) == 32

    release = threading.Event()
    def blocked(_pid, _ticks):
        while not release.is_set():
            import time; time.sleep(0.001)
        return {"cpu_capacity_percent": 1}
    monkeypatch.setattr(web_main, "probe_resource_capacity", blocked)
    tasks = [asyncio.create_task(cache.get(100 + i, STARTED, 11)) for i in range(17)]
    await asyncio.sleep(0.03)
    assert len(cache._inflight) <= 16
    release.set()
    try:
        await asyncio.gather(*tasks)
    finally:
        release.set()
