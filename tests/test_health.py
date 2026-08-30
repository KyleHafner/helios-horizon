import asyncio
import socket
from types import SimpleNamespace

import pytest

from game_control.health import (
    check_listening,
    HealthChecker,
    ReadinessCoordinator,
    ReadinessOutcome,
)
from game_control.models import HealthState, ProfileId


@pytest.mark.asyncio
async def test_internal_readiness_notification_delivers_success_and_rejects_stale_ticket():
    readiness = ReadinessCoordinator()
    stale = readiness.begin("minecraft")
    current = readiness.begin("minecraft")
    with pytest.raises(RuntimeError, match="stale"):
        readiness.latest("minecraft", generation=stale.generation)
    assert readiness.latest("minecraft", generation=current.generation) == current
    assert readiness.notify(stale, ReadinessOutcome.SUCCESS) is False
    assert readiness.notify(current, ReadinessOutcome.SUCCESS) is True
    assert await readiness.wait(current, timeout=1) is ReadinessOutcome.SUCCESS
    readiness.finish(current)
    assert readiness.health() == {"active": 0, "success": 1, "failure": 0, "timeout": 0}


@pytest.mark.asyncio
async def test_internal_readiness_notification_delivers_failure():
    readiness = ReadinessCoordinator()
    ticket = readiness.begin("minecraft")
    readiness.notify(ticket, ReadinessOutcome.FAILURE)
    assert await readiness.wait(ticket, timeout=1) is ReadinessOutcome.FAILURE


@pytest.mark.asyncio
async def test_internal_readiness_wait_is_bounded_and_records_timeout():
    readiness = ReadinessCoordinator()
    ticket = readiness.begin("minecraft")
    with pytest.raises(asyncio.TimeoutError):
        await readiness.wait(ticket, timeout=0.001)
    assert readiness.health()["timeout"] == 1


def test_tcp_and_udp_listening_are_protocol_correct(monkeypatch):
    class Conn:
        def __init__(self, kind, port, status):
            self.type = kind
            self.laddr = ("127.0.0.1", port)
            self.status = status

    monkeypatch.setattr(
        "psutil.net_connections",
        lambda kind: [Conn(socket.SOCK_STREAM, 25565, "LISTEN")] if kind == "tcp" else [Conn(socket.SOCK_DGRAM, 16261, "NONE")],
    )
    assert check_listening("tcp", 25565)
    assert check_listening("udp", 16261)
    assert not check_listening("udp", 25565)


@pytest.mark.asyncio
async def test_process_alive_with_failed_health_is_unhealthy():
    checker = HealthChecker(
        adapter=type(
            "Adapter",
            (),
            {"observe": lambda self, profile: _observation()},
        )()
        , process_checker=lambda profile, observation: True
    )
    result = await checker.check(object())
    assert result.process_alive is True
    assert result.state.value == "unhealthy"


@pytest.mark.asyncio
async def test_health_fails_closed_without_process_validator():
    checker = HealthChecker(
        adapter=type("Adapter", (), {"observe": lambda self, profile: _observation()})()
    )
    result = await checker.check(object())
    assert result.process_alive is False


@pytest.mark.asyncio
async def test_health_reuses_supplied_observation_without_reprobing_adapter():
    from game_control.adapters.base import AdapterObservation

    class Adapter:
        async def observe(self, _profile):
            raise AssertionError("health check repeated an already completed observation")

    observation = AdapterObservation(running=True, healthy=True, pid=41)
    result = await HealthChecker(
        Adapter(),
        process_checker=lambda _profile, current: current is observation,
    ).check(SimpleNamespace(), observation=observation)

    assert result.process_alive is True
    assert result.state is HealthState.HEALTHY


@pytest.mark.asyncio
async def test_health_reuses_supplied_none_observation_without_reprobing_adapter():
    class Adapter:
        async def observe(self, _profile):
            raise AssertionError("health check repeated an already completed observation")

    observed = []
    result = await HealthChecker(
        Adapter(),
        process_checker=lambda _profile, current: observed.append(current) or False,
    ).check(SimpleNamespace(), observation=None)

    assert observed == [None]
    assert result.process_alive is False


@pytest.mark.asyncio
async def test_health_adapter_error_is_unknown_and_does_not_escape():
    from game_control.adapters.base import AdapterError, AdapterObservation
    from game_control.models import HealthState

    class Adapter:
        async def observe(self, profile):
            if profile == "failing":
                raise AdapterError("systemd unavailable")
            return AdapterObservation(running=True, healthy=True)

    checker = HealthChecker(Adapter(), process_checker=lambda _p, _o: True)
    failing = await checker.check("failing")
    healthy = await checker.check(
        type(
            "Profile",
            (),
            {
                "process": type("Process", (), {"ready_log_pattern": None})(),
                "paths": type("Paths", (), {"log_files": (object(),)})(),
            },
        )()
    )
    assert failing.state is HealthState.UNKNOWN
    assert failing.process_alive is False
    assert healthy.state is HealthState.HEALTHY


@pytest.mark.asyncio
async def test_systemd_readiness_can_use_bounded_adapter_journal():
    from datetime import datetime, timezone
    from game_control.adapters.base import AdapterObservation
    from game_control.protocol import LogLine

    class Adapter:
        async def observe(self, profile):
            return AdapterObservation(running=True, pid=1, started_at=datetime(2025, 1, 1, tzinfo=timezone.utc), healthy=True)

        async def recent_logs(self, profile, limit):
            return [LogLine(timestamp=datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc), severity="info", message="READY")]

    profile = type(
        "Profile", (),
        {
            "process": type("Process", (), {"ready_log_pattern": "READY"})(),
            "paths": type("Paths", (), {"log_files": ()})(),
            "adapter": "systemd",
        },
    )()
    result = await HealthChecker(Adapter(), process_checker=lambda p, o: True).check(profile)
    assert result.ready is True


@pytest.mark.asyncio
async def test_systemd_readiness_falls_back_to_journal_when_flat_log_has_no_current_marker(tmp_path):
    from datetime import datetime, timezone
    from game_control.adapters.base import AdapterObservation
    from game_control.protocol import LogLine

    flat_log = tmp_path / "server.log"
    flat_log.write_text("stale output without a timestamped ready marker\n")

    class Adapter:
        async def observe(self, profile):
            return AdapterObservation(
                running=True,
                pid=1,
                started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                healthy=True,
            )

        async def recent_logs(self, profile, limit):
            return [
                LogLine(
                    timestamp=datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
                    severity="info",
                    message="Server started",
                )
            ]

    profile = type(
        "Profile",
        (),
        {
            "process": type("Process", (), {"ready_log_pattern": "Server started"})(),
            "paths": type("Paths", (), {"log_files": (flat_log,)})(),
            "adapter": "systemd",
        },
    )()
    result = await HealthChecker(Adapter(), process_checker=lambda p, o: True).check(profile)
    assert result.ready is True
    assert result.state.value == "healthy"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [ProfileId.TERRARIA_VANILLA, ProfileId.TERRARIA_TMOD])
async def test_terraria_relay_health_never_uses_active_public_probe(monkeypatch, profile_id):
    public_probe_calls = []
    tcp_probe_calls = []

    async def public_probe(host, port, protocol):
        public_probe_calls.append((host, port, protocol))
        return True

    async def tcp_probe(host, port):
        tcp_probe_calls.append((host, port))
        return True

    monkeypatch.setattr(HealthChecker, "_tcp_probe", staticmethod(tcp_probe))
    profile = SimpleNamespace(
        id=profile_id,
        public_endpoint=SimpleNamespace(host="terraria.example", port=7777, protocol="tcp"),
    )
    checker = HealthChecker(relay_checker=lambda endpoint: True, public_probe=public_probe)

    result = await checker.check(profile)
    assert result.relay is HealthState.UNKNOWN
    assert public_probe_calls == []
    assert tcp_probe_calls == []


@pytest.mark.asyncio
async def test_non_terraria_tcp_relay_health_keeps_active_public_probe():
    public_probe_calls = []

    async def public_probe(host, port, protocol):
        public_probe_calls.append((host, port, protocol))
        return True

    profile = SimpleNamespace(
        id=ProfileId.MINECRAFT,
        public_endpoint=SimpleNamespace(host="minecraft.example", port=25565, protocol="tcp"),
    )
    checker = HealthChecker(relay_checker=lambda endpoint: True, public_probe=public_probe)

    result = await checker.check(profile)
    assert result.relay is HealthState.HEALTHY
    assert public_probe_calls == [("minecraft.example", 25565, "tcp")]


def test_ready_marker_comes_from_bounded_current_tail(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("READY\n" + ("x" * (300 * 1024)) + "\n")
    profile = type(
        "Profile",
        (),
        {
            "process": type("Process", (), {"ready_log_pattern": "READY"})(),
            "paths": type("Paths", (), {"log_files": (path,)})(),
        },
    )()
    checker = HealthChecker()
    assert checker._ready_marker(profile) is False


def test_ready_marker_requires_timestamp_at_or_after_activation(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "server.log"
    path.write_text("2020-01-01T00:00:00Z READY\n")
    profile = type(
        "Profile",
        (),
        {
            "process": type("Process", (), {"ready_log_pattern": "READY"})(),
            "paths": type("Paths", (), {"log_files": (path,)})(),
        },
    )()
    checker = HealthChecker()
    assert checker._ready_marker(profile, datetime(2021, 1, 1, tzinfo=timezone.utc)) is False


def test_ready_marker_success_is_cached_only_for_same_activation(tmp_path):
    from datetime import datetime, timezone

    path = tmp_path / "server.log"
    path.write_text("2026-08-20T12:00:01Z READY\n")
    profile = SimpleNamespace(
        id="minecraft-sunlit-cobblemon",
        process=SimpleNamespace(ready_log_pattern="READY"),
        paths=SimpleNamespace(log_files=(path,)),
    )
    checker = HealthChecker()
    started = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    assert checker._ready_marker(profile, started) is True
    path.unlink()
    assert checker._ready_marker(profile, started) is True
    assert checker._ready_marker(
        profile, datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)
    ) is False


def test_ready_marker_cache_invalidates_on_pattern_or_log_path_change(tmp_path):
    from datetime import datetime, timezone

    first = tmp_path / "first.log"
    second = tmp_path / "second.log"
    first.write_text("2026-08-20T12:00:01Z READY\n")
    second.write_text("2026-08-20T12:00:01Z OTHER\n")
    profile = SimpleNamespace(
        id="minecraft-sunlit-cobblemon",
        process=SimpleNamespace(ready_log_pattern="READY"),
        paths=SimpleNamespace(log_files=(first,)),
    )
    checker = HealthChecker()
    started = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert checker._ready_marker(profile, started) is True
    profile.process.ready_log_pattern = "OTHER"
    profile.paths.log_files = (second,)
    assert checker._ready_marker(profile, started) is True
    profile.process.ready_log_pattern = "MISSING"
    assert checker._ready_marker(profile, started) is False


@pytest.mark.asyncio
async def test_ambiguous_adapter_health_with_validated_process_is_unknown():
    from game_control.adapters.base import AdapterObservation

    class Adapter:
        async def observe(self, _profile):
            return AdapterObservation(running=True, healthy=None)

    result = await HealthChecker(
        Adapter(),
        process_checker=lambda _profile, _observation: True,
    ).check(SimpleNamespace())

    assert result.process_alive is True
    assert result.state is HealthState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required", "expected"),
    [(True, HealthState.UNHEALTHY), (False, HealthState.HEALTHY)],
)
async def test_required_port_failure_is_distinct_from_optional_port_failure(required, expected):
    from game_control.adapters.base import AdapterObservation

    profile = SimpleNamespace(
        ports=(SimpleNamespace(protocol="tcp", port=25565, required=required),),
    )
    result = await HealthChecker(
        adapter=type("Adapter", (), {"observe": lambda self, _profile: AdapterObservation(running=True, healthy=True)})(),
        process_checker=lambda _profile, _observation: True,
        port_checker=lambda _protocol, _port: False,
    ).check(profile)

    assert result.state is expected
    assert result.required_ports is (False if required else True)
    assert result.evidence == ("validated-process", "tcp:25565:failed")


@pytest.mark.asyncio
async def test_process_validation_error_fails_closed_without_losing_adapter_ambiguity():
    from game_control.adapters.base import AdapterObservation

    def process_checker(_profile, _observation):
        raise OSError("/proc disappeared")

    result = await HealthChecker(
        adapter=type("Adapter", (), {"observe": lambda self, _profile: AdapterObservation(running=True, healthy=True)})(),
        process_checker=process_checker,
    ).check(SimpleNamespace())

    assert result.process_alive is False
    assert result.state is HealthState.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, expected", [(False, HealthState.UNHEALTHY), ("timeout", HealthState.HEALTHY)])
async def test_a2s_failure_and_timeout_are_not_the_same_health_state(outcome, expected):
    from game_control.adapters.base import AdapterObservation

    async def a2s(_profile):
        if outcome == "timeout":
            raise asyncio.TimeoutError
        return outcome

    profile = SimpleNamespace(id="pz-rising")
    result = await HealthChecker(
        adapter=type("Adapter", (), {"observe": lambda self, _profile: AdapterObservation(running=True, healthy=True)})(),
        process_checker=lambda _profile, _observation: True,
        a2s_checker=a2s,
    ).check(profile)

    assert result.state is expected
    assert "a2s:failed" in result.evidence if outcome is False else "a2s:failed" not in result.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, expected", [(False, HealthState.UNHEALTHY), ("timeout", HealthState.UNKNOWN)])
async def test_public_probe_failure_and_timeout_are_not_the_same_relay_state(outcome, expected):
    async def public_probe(_host, _port, _protocol):
        if outcome == "timeout":
            raise asyncio.TimeoutError
        return outcome

    profile = SimpleNamespace(
        id=ProfileId.MINECRAFT,
        public_endpoint=SimpleNamespace(host="minecraft.example", port=25565, protocol="tcp"),
    )
    result = await HealthChecker(
        relay_checker=lambda _endpoint: True,
        public_probe=public_probe,
    ).check(profile)

    assert result.relay is expected


def test_ready_marker_ignores_hostile_timestamp_content(tmp_path):
    path = tmp_path / "server.log"
    path.write_bytes(b"not-a-timestamp READY\n\xff\xfe\n")
    profile = SimpleNamespace(
        process=SimpleNamespace(ready_log_pattern="READY"),
        paths=SimpleNamespace(log_files=(path,)),
    )

    assert HealthChecker()._ready_marker(profile, object()) is False


async def _observation():
    from game_control.adapters.base import AdapterObservation

    return AdapterObservation(running=True, healthy=False, pid=10)
