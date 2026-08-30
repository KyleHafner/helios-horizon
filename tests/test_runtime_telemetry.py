from __future__ import annotations

import asyncio
import inspect
import math
import sqlite3
import threading
import time
from types import SimpleNamespace
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from game_control.runtime.protocols import (
    AlertObservation,
    AlertSink,
    StatusSnapshotProvider,
    TelemetryCollector,
    TelemetryDatabaseWriter,
    TelemetrySampler,
)
from game_control.runtime.telemetry import (
    DEFAULT_HOST_METRICS,
    ExporterBinding,
    GcLogBinding,
    LegacyTpsMode,
    TelemetryRuntimeConfig,
    TelemetryCollector as RuntimeTelemetryCollector,
    TelemetryRuntime,
    ResourceRef,
)
from game_control.telemetry_db import TelemetryDatabase


def _settings(**overrides):
    settings = {
        "exporter_url": "http://127.0.0.1:19565/metrics",
        "tick_profile": "minecraft-sunlit-cobblemon",
        "log_checkpoint_dir": "/var/lib/game-control/log-checkpoints",
        "gc_profile_id": "minecraft-sunlit-cobblemon",
        "gc_log_path": "/srv/game-servers/minecraft-sunlit-cobblemon/logs/gc.log",
        "legacy_tps_mode": "disabled",
    }
    settings.update(overrides)
    return settings


def test_root_config_is_frozen_and_requires_explicit_legacy_mode():
    config = TelemetryRuntimeConfig.from_root_config(
        _settings(), approved_profile_ids=("minecraft-sunlit-cobblemon", "terraria-vanilla")
    )
    assert config.legacy_tps_mode is LegacyTpsMode.DISABLED
    assert config.exporters == (
        ExporterBinding("minecraft-sunlit-cobblemon", "http://127.0.0.1:19565/metrics"),
    )
    assert config.host_metrics == DEFAULT_HOST_METRICS
    assert config.gc_log == GcLogBinding(
        "minecraft-sunlit-cobblemon", Path("/srv/game-servers/minecraft-sunlit-cobblemon/logs/gc.log")
    )
    with pytest.raises(FrozenInstanceError):
        config.legacy_tps_mode = LegacyTpsMode.ENABLED  # type: ignore[misc]
    with pytest.raises(ValueError, match="explicit"):
        TelemetryRuntimeConfig.from_root_config(
            {key: value for key, value in _settings().items() if key != "legacy_tps_mode"},
            approved_profile_ids=("minecraft-sunlit-cobblemon",),
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"legacy_tps_mode": "disabled", "unknown": True},
        _settings(exporter_url="https://example.test/metrics"),
        _settings(exporter_url="http://127.0.0.1:19565/private"),
        _settings(tick_metric="browser-selected"),
        _settings(tick_profile="browser-selected"),
        _settings(log_checkpoint_dir="relative/path"),
        _settings(gc_log_path="/srv/game/../other/gc.log"),
        _settings(gc_profile_id=None),
        _settings(gc_profile_id="not-approved"),
        _settings(host_metrics=("arbitrary_metric",)),
    ],
)
def test_root_config_rejects_unknown_or_unapproved_values(settings):
    with pytest.raises(ValueError):
        TelemetryRuntimeConfig.from_root_config(
            settings, approved_profile_ids=("minecraft-sunlit-cobblemon",)
        )


def test_root_config_does_not_use_exporter_presence_as_legacy_switch():
    disabled = TelemetryRuntimeConfig.from_root_config(
        _settings(legacy_tps_mode="disabled"), approved_profile_ids=("minecraft-sunlit-cobblemon",)
    )
    enabled = TelemetryRuntimeConfig.from_root_config(
        _settings(legacy_tps_mode="enabled"), approved_profile_ids=("minecraft-sunlit-cobblemon",)
    )
    assert disabled.legacy_tps_mode is LegacyTpsMode.DISABLED
    assert enabled.legacy_tps_mode is LegacyTpsMode.ENABLED
    assert disabled.exporters == enabled.exporters


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profile_id": "", "profile_state": "running", "now": 1},
        {"profile_id": "../../etc/passwd", "profile_state": "running", "now": 1},
        {"profile_id": "https://evil.invalid", "profile_state": "running", "now": 1},
        {"profile_id": "profile with spaces", "profile_state": "running", "now": 1},
        {"profile_id": "profile\twith-control", "profile_state": "running", "now": 1},
        {"profile_id": "profile_é", "profile_state": "running", "now": 1},
        {"profile_id": "1-profile", "profile_state": "running", "now": 1},
        {"profile_id": "p" * 129, "profile_state": "running", "now": 1},
        {"profile_id": "minecraft", "profile_state": "unknown", "now": 1},
        {"profile_id": "minecraft", "profile_state": "running", "now": math.nan},
        {"profile_id": "minecraft", "profile_state": "running", "now": -1},
        {"profile_id": "minecraft", "profile_state": "running", "now": 1, "mspt_p95": -1},
        {"profile_id": "minecraft", "profile_state": "running", "now": 1, "benchmark_regression": 1},
    ],
)
def test_alert_observation_is_bounded(kwargs):
    with pytest.raises(ValueError):
        AlertObservation(**kwargs)


def test_alert_observation_has_only_typed_bounded_fields():
    observation = AlertObservation(
        "minecraft",
        "running",
        1.0,
        mspt_p95=51,
        rss_bytes=1024,
        wake_duration_ms=None,
        benchmark_regression=False,
    )
    assert observation.profile_id == "minecraft"
    assert observation.benchmark_regression is False
    with pytest.raises(TypeError):
        AlertObservation("minecraft", "running", 1.0, arbitrary="event")  # type: ignore[call-arg]


def test_protocols_are_runtime_contracts_without_forbidden_back_edges():
    for protocol in (AlertSink, StatusSnapshotProvider, TelemetryCollector, TelemetryDatabaseWriter, TelemetrySampler):
        assert getattr(protocol, "_is_runtime_protocol", False)
    source = Path(inspect.getfile(__import__("game_control.runtime.protocols", fromlist=["AlertSink"]))).read_text()
    telemetry_source = Path(inspect.getfile(__import__("game_control.runtime.telemetry", fromlist=["TelemetryRuntimeConfig"]))).read_text()
    for forbidden in ("service_wiring", "service_container", "controller", "runtime.alerts"):
        assert f"from .{forbidden}" not in source
        assert f"from .{forbidden}" not in telemetry_source


@pytest.mark.asyncio
async def test_single_cycle_uses_one_snapshot_and_one_telemetry_writer_thread(tmp_path):
    database = TelemetryDatabase.open(tmp_path / "telemetry.db")
    owner_thread = threading.get_ident()
    worker_threads = set()
    stamp = int(time.time() * 1000)
    process_sample = SimpleNamespace(cpu_percent=2.0, rss_bytes=100, disk_read_bps=3.0, disk_write_bps=4.0)
    original_process = database._write_with
    original_generic = database._write_generic_with

    def write_process(*args, **kwargs):
        worker_threads.add(threading.get_ident())
        return original_process(*args, **kwargs)

    def write_generic(*args, **kwargs):
        worker_threads.add(threading.get_ident())
        return original_generic(*args, **kwargs)

    database._write_with = write_process
    database._write_generic_with = write_generic
    snapshot = SimpleNamespace(profiles=(SimpleNamespace(
        profile_id="minecraft", state="running", pid=1, rss_bytes=100,
    ),))
    calls = []

    class Status:
        async def snapshot(self, *, persist=False, force=False):
            calls.append((persist, force))
            assert persist is True and force is True
            assert database.enqueue_process_sample(
                "minecraft", process_sample, ts_ms=stamp, state="available"
            )
            return snapshot

    profile = SimpleNamespace(id="minecraft", systemd_unit=None, paths=SimpleNamespace(log_files=()))
    generic_stamps = iter((stamp + 1, stamp + 2))
    config = TelemetryRuntimeConfig(
        host_metrics=("host_psi_io_some_avg10", "host_psi_io_full_avg10"),
        legacy_tps_mode="disabled",
    )
    collector = RuntimeTelemetryCollector(
        profiles=(profile,), config=config, database=ResourceRef.borrowed(database),
        rcon=None, player_tracker=SimpleNamespace(),
        wall_clock_ms=lambda: next(generic_stamps),
    )
    runtime = TelemetryRuntime(Status(), collector)
    try:
        await runtime.sample_once()
        await runtime.close()
        assert calls == [(True, True)]
        assert len(worker_threads) == 1
        assert owner_thread not in worker_threads
        reader = sqlite3.connect(database.path)
        try:
            assert reader.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0] == 6
            assert reader.execute(
                "SELECT count(*) FROM telemetry_samples WHERE ts_ms=?", (stamp,)
            ).fetchone()[0] == 4
            assert reader.execute(
                "SELECT count(*) FROM telemetry_samples WHERE ts_ms=?", (stamp + 1,)
            ).fetchone()[0] == 1
            assert reader.execute(
                "SELECT count(*) FROM telemetry_samples WHERE ts_ms=?", (stamp + 2,)
            ).fetchone()[0] == 1
        finally:
            reader.close()
    finally:
        database.close()


@pytest.mark.asyncio
async def test_runtime_cancellation_drains_status_without_late_cycle():
    started = asyncio.Event()
    release = asyncio.Event()
    cycles = []

    class Status:
        async def snapshot(self, *, persist=False, force=False):
            started.set()
            await release.wait()
            return object()

    class Collector:
        async def collect(self, snapshot):
            cycles.append(snapshot)

        async def close(self):
            return None

        def health(self):
            return {}

    runtime = TelemetryRuntime(Status(), Collector())
    sample = asyncio.create_task(runtime.sample_once())
    await started.wait()
    sample.cancel()
    await asyncio.sleep(0)
    assert not sample.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await sample
    assert cycles == []
    await runtime.close()


@pytest.mark.asyncio
async def test_resource_ownership_is_explicit_for_sampler_rcon_and_database():
    class Database:
        def __init__(self):
            self.drains = 0
            self.closes = 0

        def close(self):
            self.closes += 1

        def drain(self, _timeout=None):
            self.drains += 1
            return True

    class Sampler:
        def __init__(self):
            self.shutdowns = 0
            self.waits = 0

        async def shutdown(self):
            self.shutdowns += 1

        async def wait_closed(self):
            self.waits += 1

    class Rcon:
        profile_id = "minecraft"

        def __init__(self):
            self.closes = 0

        async def close(self):
            self.closes += 1

    borrowed_db, owned_db = Database(), Database()
    borrowed_rcon, owned_rcon = Rcon(), Rcon()
    borrowed_sampler, owned_sampler = Sampler(), Sampler()
    config = TelemetryRuntimeConfig(
        host_metrics=("host_psi_io_some_avg10",), legacy_tps_mode="disabled"
    )

    borrowed_collector = RuntimeTelemetryCollector(
        profiles=(), config=config, database=borrowed_db, rcon=borrowed_rcon,
        player_tracker=SimpleNamespace(),
    )
    owned_collector = RuntimeTelemetryCollector(
        profiles=(), config=config, database=ResourceRef.owned(owned_db),
        rcon=ResourceRef.owned(owned_rcon), player_tracker=SimpleNamespace(),
    )
    await borrowed_collector.close()
    await owned_collector.close()
    assert borrowed_db.closes == borrowed_rcon.closes == 0
    assert borrowed_db.drains == 1
    assert owned_db.closes == owned_rcon.closes == 1

    class Collector:
        async def collect(self, _snapshot):
            return None

        async def close(self):
            return None

        def health(self):
            return {}

    borrowed_runtime = TelemetryRuntime(
        SimpleNamespace(snapshot=lambda **_: None), Collector(), sampler=borrowed_sampler,
    )
    owned_runtime = TelemetryRuntime(
        SimpleNamespace(snapshot=lambda **_: None), Collector(), sampler=ResourceRef.owned(owned_sampler),
    )
    await borrowed_runtime.close()
    await owned_runtime.close()
    assert borrowed_sampler.shutdowns == borrowed_sampler.waits == 0
    assert owned_sampler.shutdowns == owned_sampler.waits == 1


@pytest.mark.asyncio
async def test_runtime_close_drains_owned_cleanup_before_reraising_cancellation():
    started = asyncio.Event()
    release = asyncio.Event()

    class Collector:
        async def collect(self, _snapshot):
            return None

        async def close(self):
            started.set()
            await release.wait()

        def health(self):
            return {}

    runtime = TelemetryRuntime(SimpleNamespace(snapshot=lambda **_: None), Collector())
    closing = asyncio.create_task(runtime.close())
    await started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert runtime.health()["closed"] is True
