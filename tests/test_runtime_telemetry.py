from __future__ import annotations

import inspect
import math
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
    LegacyTpsMode,
    TelemetryRuntimeConfig,
)


def _settings(**overrides):
    settings = {
        "exporter_url": "http://127.0.0.1:19565/metrics",
        "tick_profile": "minecraft-sunlit-cobblemon",
        "log_checkpoint_dir": "/var/lib/game-control/log-checkpoints",
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
