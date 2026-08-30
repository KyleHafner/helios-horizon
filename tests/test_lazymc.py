from __future__ import annotations

import json
from pathlib import Path

import pytest

from game_control.capability import CAPABILITY_START_PROFILE
from game_control.lazymc import (
    CAPABILITY_STATUS_URL,
    CAPABILITY_WAKE_URL,
    LazyWakeError,
    LazyWakeClient,
)


class _Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


def _healthy():
    return {
        "profiles": [
            {
                "profile_id": CAPABILITY_START_PROFILE.value,
                "state": "running",
                "health": "healthy",
                "required_ports_ready": True,
            }
        ]
    }


def _stopped():
    return {
        "profiles": [
            {
                "profile_id": CAPABILITY_START_PROFILE.value,
                "state": "stopped",
                "health": "unknown",
                "required_ports_ready": False,
            }
        ]
    }


def test_lazymc_client_uses_only_fixed_typed_endpoints_and_waits_for_health():
    assert CAPABILITY_WAKE_URL == "http://192.0.2.10:8444/api/v1/capability/wake"
    assert CAPABILITY_STATUS_URL == "http://192.0.2.10:8444/api/v1/capability/status"
    calls = []
    responses = iter([{"state": "ready", "readiness_generation": 7}])

    def opener(request, timeout):
        calls.append((request.full_url, timeout, json.loads(request.data), dict(request.headers)))
        return _Response(next(responses))

    client = LazyWakeClient("hc_" + "a" * 44, opener=opener)
    client.wake_and_wait(grace_seconds=10)
    assert [item[0] for item in calls] == [CAPABILITY_WAKE_URL]
    assert all(item[2]["action"]["kind"] == "wake" for item in calls)
    assert all("profile_id" not in item[2]["action"] for item in calls)
    assert all(item[3]["X-horizon-capability-audience"] == "lazymc" for item in calls)


def test_supervisor_waits_for_controller_idle_stop_without_java_or_backend_control():
    calls = []
    responses = iter([{"state": "ready", "readiness_generation": 7}, _stopped()])

    def opener(request, timeout):
        calls.append((request.full_url, json.loads(request.data)))
        return _Response(next(responses))

    client = LazyWakeClient("hc_" + "a" * 44, opener=opener, sleep=lambda _seconds: None)
    client.wake_and_wait(grace_seconds=10)
    client.wait_until_stopped()

    assert [url for url, _body in calls] == [
        CAPABILITY_WAKE_URL,
        CAPABILITY_STATUS_URL,
    ]
    assert all(url in {CAPABILITY_WAKE_URL, CAPABILITY_STATUS_URL} for url, _body in calls)
    assert all("25566" not in url and "java" not in url.casefold() for url, _body in calls)
    assert all(set(body) == {"request_id", "action"} for _url, body in calls)
    assert all(set(body["action"]) == {"kind"} for _url, body in calls)


def test_supervisor_fails_closed_on_controller_failure_without_restart_path():
    responses = iter([{"error": {"message": "failed"}}])

    def opener(request, timeout):
        return _Response(next(responses))

    client = LazyWakeClient("hc_" + "a" * 44, opener=opener, sleep=lambda _seconds: None)
    with pytest.raises(LazyWakeError):
        client.wake_and_wait(grace_seconds=10)


def test_wake_transport_failure_retries_same_bounded_edge_call():
    calls = []
    responses = iter([OSError("busy"), {"state": "ready", "readiness_generation": 9}])

    def opener(request, timeout):
        calls.append((request.full_url, json.loads(request.data), timeout))
        response = next(responses)
        if isinstance(response, OSError):
            raise response
        return _Response(response)

    client = LazyWakeClient("hc_" + "a" * 44, opener=opener, sleep=lambda _seconds: None)
    client.wake_and_wait(grace_seconds=10)
    assert [item[0] for item in calls] == [CAPABILITY_WAKE_URL, CAPABILITY_WAKE_URL]
    assert calls[0][1] == calls[1][1]
    assert all(item[1]["action"] == {"kind": "wake"} for item in calls)
    assert all(1 <= item[2] <= 10 for item in calls)


def test_lazymc_packaging_keeps_public_and_backend_ports_and_disables_crash_restart():
    config = (Path(__file__).parents[1] / "ops/lazymc/lazymc.toml").read_text()
    unit = (Path(__file__).parents[1] / "ops/systemd/lazymc-minecraft.service").read_text()
    helper = (Path(__file__).parents[1] / "ops/bin/horizon-lazymc-wake").read_text()
    metadata = (Path(__file__).parents[1] / "ops/lazymc/server.properties").read_text()
    assert helper.startswith("#!/opt/game-control/.venv/bin/python\n")
    assert 'address = "0.0.0.0:25565"' in config
    assert 'address = "127.0.0.1:25566"' in config
    assert 'command = "/usr/local/libexec/horizon-lazymc-wake"' in config
    assert 'directory = "/etc/game-control/lazymc"' in config
    assert "/srv/game-servers" not in config
    assert "server-ip=127.0.0.1" in metadata
    assert "server-port=25566" in metadata
    assert "freeze_process = false" in config
    assert "wake_on_start = false" in config
    assert "wake_on_crash = false" in config
    assert "sleep_after = 4294967295" in config
    assert "rewrite_server_properties = false" in config
    assert "methods = [\"hold\", \"kick\"]" in config
    assert "run_wake_hook" in helper
    assert "subprocess" not in helper
    assert "systemctl" not in helper
    assert "Restart=no" in unit
    unit_section = unit.split("[Unit]", 1)[1].split("[Service]", 1)[0]
    service_section = unit.split("[Service]", 1)[1]
    assert "Wants=network-online.target game-control-web.service" in unit_section
    assert "Requires=game-control-web.service" not in unit_section
    assert "StartLimitIntervalSec=300" in unit_section
    assert "StartLimitBurst=1" in unit_section
    assert "StartLimit" not in service_section
    assert "25565" in (Path(__file__).parents[1] / "ops/systemd/bore-minecraft-fenced.service").read_text()
