"""Frozen root configuration and source contracts for telemetry runtime work.

This commit intentionally defines no collector loops and performs no wiring.
The later runtime extraction consumes these records without accepting values
from RPC or browser requests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .protocols import (
    AlertSink,
    StatusSnapshotProvider,
    TelemetryCollector,
    TelemetryDatabaseWriter,
    TelemetrySampler,
    _is_safe_identifier,
)


_MAX_PATH_LENGTH = 4096

DEFAULT_HOST_METRICS = (
    "service_io_read_bytes_total",
    "service_io_write_bytes_total",
    "service_io_read_ops_total",
    "service_io_write_ops_total",
    "host_psi_io_some_avg10",
    "host_psi_io_full_avg10",
    "host_disk_read_io_time_ms_total",
    "host_disk_write_io_time_ms_total",
    "host_network_rx_bytes_total",
    "host_network_tx_bytes_total",
)
_HOST_METRIC_SET = frozenset(DEFAULT_HOST_METRICS)
_ALLOWED_ROOT_KEYS = frozenset(
    {
        "exporter_url",
        "tick_profile",
        "log_checkpoint_dir",
        "gc_log_path",
        "host_metrics",
        "legacy_tps_mode",
        "legacy_tps_interval_seconds",
    }
)


class LegacyTpsMode(StrEnum):
    """Explicit compatibility selection for legacy state-db TPS sampling."""

    DISABLED = "disabled"
    ENABLED = "enabled"


def _safe_path(value: Any, *, name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} must be an absolute root path")
    raw = str(value)
    if not raw or len(raw) > _MAX_PATH_LENGTH or "\x00" in raw:
        raise ValueError(f"{name} must be an absolute root path")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute confined path")
    return path


def _safe_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("exporter URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("exporter URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/metrics"
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("exporter URL is not an approved loopback metrics endpoint")
    return value


def _safe_identifier(value: Any, *, name: str) -> str:
    if not _is_safe_identifier(value):
        raise ValueError(f"{name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ExporterBinding:
    """One fixed-profile, root-configured exporter source."""

    profile_id: str
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _safe_identifier(self.profile_id, name="exporter profile"))
        object.__setattr__(self, "url", _safe_url(self.url))


@dataclass(frozen=True, slots=True)
class TelemetryRuntimeConfig:
    """Immutable, fixed-topology telemetry configuration.

    Instances used by production are created with :meth:`from_root_config` by
    the root composition path.  The method accepts a narrow TOML section and
    rejects unknown keys; it is not a browser/API configuration surface.
    """

    exporters: tuple[ExporterBinding, ...] = ()
    log_checkpoint_dir: Path | None = None
    gc_log_path: Path | None = None
    host_metrics: tuple[str, ...] = DEFAULT_HOST_METRICS
    legacy_tps_mode: LegacyTpsMode = LegacyTpsMode.DISABLED
    legacy_tps_interval_seconds: float = 30.0
    resource_interval_seconds: float = 5.0
    tick_interval_seconds: float = 10.0

    def __post_init__(self) -> None:
        exporters = tuple(self.exporters)
        if len(exporters) > 16 or any(not isinstance(item, ExporterBinding) for item in exporters):
            raise ValueError("exporter bindings are invalid")
        if len({item.profile_id for item in exporters}) != len(exporters):
            raise ValueError("duplicate exporter profile binding")
        object.__setattr__(self, "exporters", exporters)

        if self.log_checkpoint_dir is not None:
            object.__setattr__(self, "log_checkpoint_dir", _safe_path(self.log_checkpoint_dir, name="log checkpoint directory"))
        if self.gc_log_path is not None:
            object.__setattr__(self, "gc_log_path", _safe_path(self.gc_log_path, name="GC log path"))

        try:
            metrics = tuple(self.host_metrics)
        except TypeError as exc:
            raise ValueError("host metric registry is not approved") from exc
        if (
            not metrics
            or len(metrics) > len(DEFAULT_HOST_METRICS)
            or any(not isinstance(metric, str) or metric not in _HOST_METRIC_SET for metric in metrics)
        ):
            raise ValueError("host metric registry is not approved")
        if len(set(metrics)) != len(metrics):
            raise ValueError("host metric registry contains duplicates")
        object.__setattr__(self, "host_metrics", metrics)

        try:
            mode = self.legacy_tps_mode if isinstance(self.legacy_tps_mode, LegacyTpsMode) else LegacyTpsMode(self.legacy_tps_mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("legacy TPS mode is invalid") from exc
        object.__setattr__(self, "legacy_tps_mode", mode)
        for value, field_name, label, minimum, maximum in (
            (self.legacy_tps_interval_seconds, "legacy_tps_interval_seconds", "legacy TPS interval", 1.0, 3600.0),
            (self.resource_interval_seconds, "resource_interval_seconds", "resource interval", 0.1, 60.0),
            (self.tick_interval_seconds, "tick_interval_seconds", "tick interval", 0.1, 300.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not minimum <= float(value) <= maximum:
                raise ValueError(f"{label} is invalid")
            object.__setattr__(self, field_name, float(value))

    @classmethod
    def from_root_config(
        cls,
        settings: Mapping[str, Any],
        *,
        approved_profile_ids: Iterable[str],
    ) -> "TelemetryRuntimeConfig":
        """Parse the narrow root TOML ``[stats]`` projection.

        The explicit mode is required so exporter presence can never silently
        select the legacy scheduler.  Profile IDs must already be present in
        the root-owned registry supplied by the composition root.
        """

        if not isinstance(settings, Mapping):
            raise ValueError("root telemetry configuration is invalid")
        unknown = set(settings) - _ALLOWED_ROOT_KEYS
        if unknown:
            raise ValueError("root telemetry configuration contains unknown keys")
        if "legacy_tps_mode" not in settings:
            raise ValueError("legacy TPS mode must be explicit")
        approved = frozenset(_safe_identifier(item, name="approved profile") for item in approved_profile_ids)

        exporter_url = settings.get("exporter_url")
        tick_profile = settings.get("tick_profile")
        exporters: tuple[ExporterBinding, ...] = ()
        if exporter_url is not None:
            profile_id = _safe_identifier(tick_profile, name="tick profile")
            if profile_id not in approved:
                raise ValueError("tick profile is not in the approved root registry")
            exporters = (ExporterBinding(profile_id, exporter_url),)
        elif tick_profile is not None:
            raise ValueError("tick profile requires an exporter URL")

        raw_metrics = settings.get("host_metrics", DEFAULT_HOST_METRICS)
        if not isinstance(raw_metrics, (list, tuple)):
            raise ValueError("host metric registry is invalid")
        return cls(
            exporters=exporters,
            log_checkpoint_dir=None if settings.get("log_checkpoint_dir") is None else _safe_path(settings["log_checkpoint_dir"], name="log checkpoint directory"),
            gc_log_path=None if settings.get("gc_log_path") is None else _safe_path(settings["gc_log_path"], name="GC log path"),
            host_metrics=tuple(raw_metrics),
            legacy_tps_mode=settings["legacy_tps_mode"],
            legacy_tps_interval_seconds=settings.get("legacy_tps_interval_seconds", 30.0),
        )


__all__ = [
    "AlertSink",
    "DEFAULT_HOST_METRICS",
    "ExporterBinding",
    "LegacyTpsMode",
    "StatusSnapshotProvider",
    "TelemetryCollector",
    "TelemetryDatabaseWriter",
    "TelemetryRuntimeConfig",
    "TelemetrySampler",
]
