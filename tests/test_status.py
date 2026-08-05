from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control.models import ObservedState
from game_control.status import StatusService, derive_state


@pytest.mark.asyncio
async def test_status_coalesces_socket_enumeration_per_protocol_per_snapshot():
    calls: list[str] = []
    tcp_rows = [SimpleNamespace(laddr=("127.0.0.1", 25565), status="LISTEN", pid=11)]
    udp_rows = [SimpleNamespace(laddr=("127.0.0.1", 16261), status="NONE", pid=12)]

    def connections(*, kind):
        calls.append(kind)
        return tcp_rows if kind == "tcp" else udp_rows

    class Adapter:
        async def observe(self, _profile):
            return SimpleNamespace(running=True, healthy=True, pid=11)

    class Health:
        async def check(self, _profile, *, connections=None):
            assert connections == {"tcp": tcp_rows, "udp": udp_rows}
            return SimpleNamespace(state="healthy", process_alive=True, required_ports=True)

    class Metrics:
        def sample(self, _profile, *, pid=None, connections=None):
            assert connections == {"tcp": tcp_rows, "udp": udp_rows}
            return SimpleNamespace(pid=pid)

    profiles = [
        SimpleNamespace(id="minecraft", ports=(SimpleNamespace(protocol="tcp", port=25565),)),
        SimpleNamespace(id="pz-rising", ports=(SimpleNamespace(protocol="udp", port=16261),)),
    ]
    snapshot = await StatusService(
        profiles,
        adapter=Adapter(),
        health_checker=Health(),
        metrics=Metrics(),
        connection_provider=connections,
    ).snapshot()

    assert len(snapshot.profiles) == 2
    assert calls == ["tcp", "udp"]


@pytest.mark.asyncio
async def test_status_skips_full_probes_for_profiles_outside_active_slot():
    observe_calls: list[str] = []
    health_calls: list[str] = []
    metric_calls: list[str] = []
    connection_calls: list[str] = []

    owner = SimpleNamespace(id="minecraft", ports=(SimpleNamespace(protocol="tcp", port=25565),))
    stopped = SimpleNamespace(
        id="pz-rising",
        installed_version="42.13",
        ports=(SimpleNamespace(protocol="udp", port=16261),),
    )

    class Adapter:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        async def observe(self, _profile):
            observe_calls.append(self.profile_id)
            return SimpleNamespace(running=self.profile_id == "minecraft", healthy=True, pid=41)

    class Health:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        async def check(self, _profile, *, connections=None):
            health_calls.append(self.profile_id)
            return SimpleNamespace(state="healthy", process_alive=True, required_ports=True)

    class Metrics:
        def sample(self, profile, *, pid=None, connections=None):
            metric_calls.append(profile.id)
            return SimpleNamespace(pid=pid, rss_bytes=123, cpu_percent=1)

    def connections(*, kind):
        connection_calls.append(kind)
        return []

    snapshot = await StatusService(
        [owner, stopped],
        adapters={"minecraft": Adapter("minecraft"), "pz-rising": Adapter("pz-rising")},
        slot_observer=lambda: SimpleNamespace(owner="minecraft"),
        health_checker={"minecraft": Health("minecraft"), "pz-rising": Health("pz-rising")},
        metrics=Metrics(),
        connection_provider=connections,
    ).snapshot()

    assert observe_calls == ["minecraft"]
    assert health_calls == ["minecraft"]
    assert metric_calls == ["minecraft"]
    assert connection_calls == ["tcp"]
    assert snapshot.profiles[0].state.value == "running"
    assert snapshot.profiles[1].state.value == "blocked"
    assert snapshot.profiles[1].installed_version == "42.13"
    assert snapshot.profiles[1].pid is None
    assert snapshot.profiles[1].rss_bytes is None
    assert snapshot.profiles[1].cpu_percent is None


@pytest.mark.asyncio
async def test_status_uses_cached_disk_metrics_for_non_owner_profiles():
    owner = SimpleNamespace(id="minecraft", ports=())
    stopped = SimpleNamespace(id="pz-rising", ports=())

    class Metrics:
        def cached_disk_metrics(self, profile):
            return SimpleNamespace(profile_data_free_bytes=987654321)

        def sample(self, profile, **kwargs):
            return SimpleNamespace(pid=None)

    snapshot = await StatusService(
        [owner, stopped],
        slot_observer=lambda: SimpleNamespace(owner="minecraft"),
        metrics=Metrics(),
    ).snapshot()

    assert snapshot.profiles[1].disk_free_bytes == 987654321
    assert snapshot.profiles[1].disk_read_bps is None
    assert snapshot.profiles[1].disk_write_bps is None


@pytest.mark.asyncio
async def test_status_caches_idle_installed_version_by_file_mtime(tmp_path, monkeypatch):
    version_file = tmp_path / "version"
    version_file.write_text("42.13\n")
    reads: list[Path] = []
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == version_file:
            reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    profile = SimpleNamespace(
        id="pz-rising",
        paths=SimpleNamespace(version_file=version_file),
    )
    service = StatusService(
        [profile],
        slot_observer=lambda: SimpleNamespace(owner="minecraft"),
    )

    first = await service.snapshot()
    second = await service.snapshot()

    assert first.profiles[0].installed_version == "42.13"
    assert second.profiles[0].installed_version == "42.13"
    assert reads == [version_file]


@pytest.mark.asyncio
async def test_cached_snapshot_does_not_resample_the_status_pipeline():
    calls = 0

    class Adapter:
        async def observe(self, _profile):
            nonlocal calls
            calls += 1
            return type("Observation", (), {"running": False, "healthy": None})()

    profile = type("Profile", (), {"id": "minecraft"})()
    service = StatusService([profile], adapter=Adapter())

    fresh = await service.snapshot()
    cached = await service.cached_snapshot()

    assert cached is fresh
    assert calls == 1


@pytest.mark.parametrize(
    ("job", "process", "slot_other", "expected"),
    [
        ("start", False, False, "starting"),
        (None, True, False, "running"),
        ("stop", True, False, "stopping"),
        (None, False, True, "blocked"),
        ("failed", False, False, "failed"),
        (None, False, False, "stopped"),
    ],
)
def test_state_precedence(job, process, slot_other, expected):
    assert derive_state(
        active_job=job,
        process_alive=process,
        conflicting_slot_owner=slot_other,
    ) is ObservedState(expected)


def test_controller_process_does_not_make_minecraft_running():
    assert derive_state(active_job=None, process_alive=False, conflicting_slot_owner=False) is ObservedState.STOPPED
    assert derive_state(active_job=None, process_alive=True, conflicting_slot_owner=False) is ObservedState.RUNNING


@pytest.mark.asyncio
async def test_status_keeps_live_process_running_when_health_fails():
    from game_control.adapters.base import AdapterObservation
    from game_control.health import HealthResult
    from game_control.models import HealthState

    profile = type("Profile", (), {"id": "minecraft"})()

    class Adapter:
        async def observe(self, _profile):
            return AdapterObservation(running=True, healthy=False, pid=42)

    class Health:
        async def check(self, _profile):
            return HealthResult(state=HealthState.UNHEALTHY, process_alive=True)

    snapshot = await StatusService(
        [profile],
        adapter=Adapter(),
        health_checker=Health(),
    ).snapshot()
    assert snapshot.profiles[0].state is ObservedState.RUNNING
    assert snapshot.profiles[0].health is HealthState.UNHEALTHY


@pytest.mark.asyncio
async def test_status_uses_validated_health_process_identity():
    from game_control.adapters.base import AdapterObservation
    from game_control.health import HealthResult
    from game_control.models import HealthState

    profile = type("Profile", (), {"id": "minecraft"})()

    class Adapter:
        async def observe(self, _profile):
            # Crafty controller says "running", but no validated JVM exists.
            return AdapterObservation(running=True, healthy=None, pid=None)

    class Health:
        async def check(self, _profile):
            return HealthResult(state=HealthState.UNKNOWN, process_alive=False)

    snapshot = await StatusService([profile], adapter=Adapter(), health_checker=Health()).snapshot()
    assert snapshot.profiles[0].state is ObservedState.STOPPED


@pytest.mark.asyncio
async def test_status_reports_only_sampled_pid():
    from game_control.adapters.base import AdapterObservation
    from game_control.health import HealthResult
    from game_control.models import HealthState

    profile = type("Profile", (), {"id": "minecraft"})()
    adapter = type("Adapter", (), {"observe": lambda self, p: AdapterObservation(running=True, pid=99)})()
    health = type("Health", (), {"check": lambda self, p: HealthResult(HealthState.HEALTHY, True)})()
    metrics = type("Metrics", (), {"sample": lambda self, p, pid=None: type("M", (), {"pid": None})()})()
    snapshot = await StatusService([profile], adapter=adapter, health_checker=health, metrics=metrics).snapshot()
    assert snapshot.profiles[0].pid is None


@pytest.mark.asyncio
async def test_status_missing_validated_checker_fails_closed():
    from game_control.adapters.base import AdapterObservation

    profile = type("Profile", (), {"id": "minecraft"})()
    adapter = type("Adapter", (), {"observe": lambda self, p: AdapterObservation(running=True, pid=42)})()
    snapshot = await StatusService([profile], adapter=adapter).snapshot()
    assert snapshot.profiles[0].state is ObservedState.STOPPED


@pytest.mark.asyncio
async def test_status_adapter_error_degrades_only_failing_profile():
    from game_control.adapters.base import AdapterError, AdapterObservation
    from game_control.models import HealthState

    failing = type("Profile", (), {"id": "minecraft"})()
    healthy = type("Profile", (), {"id": "pz-rising"})()

    class FailingAdapter:
        async def observe(self, _profile):
            raise AdapterError("journal unavailable")

    class HealthyAdapter:
        async def observe(self, _profile):
            return AdapterObservation(running=True, healthy=True)

    snapshot = await StatusService(
        [failing, healthy],
        adapters={"minecraft": FailingAdapter(), "pz-rising": HealthyAdapter()},
    ).snapshot()
    assert snapshot.profiles[0].state is ObservedState.STOPPED
    assert snapshot.profiles[0].health is HealthState.UNKNOWN
    assert snapshot.profiles[1].health is HealthState.HEALTHY


@pytest.mark.asyncio
async def test_status_health_error_degrades_only_failing_profile():
    from game_control.adapters.base import AdapterObservation
    from game_control.health import HealthResult
    from game_control.models import HealthState

    failing = type("Profile", (), {"id": "minecraft"})()
    healthy = type("Profile", (), {"id": "pz-rising"})()

    class Adapter:
        async def observe(self, _profile):
            return AdapterObservation(running=True, healthy=None)

    class Health:
        async def check(self, profile):
            if profile.id == "minecraft":
                raise RuntimeError("health unavailable")
            return HealthResult(HealthState.HEALTHY, process_alive=True)

    snapshot = await StatusService(
        [failing, healthy],
        adapter=Adapter(),
        health_checker=Health(),
    ).snapshot()
    assert snapshot.profiles[0].state is ObservedState.STOPPED
    assert snapshot.profiles[0].health is HealthState.UNKNOWN
    assert snapshot.profiles[1].state is ObservedState.RUNNING
    assert snapshot.profiles[1].health is HealthState.HEALTHY
