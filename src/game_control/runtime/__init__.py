"""Typed seams for the slotd runtime extraction.

The runtime package intentionally contains contracts and small policy-free
records.  Construction and dependency selection remain in the composition
root; domain services do not import this package's future container.
"""

from .protocols import (
    AlertObservation,
    AlertSink,
    StatusSnapshotProvider,
    TelemetryCollector as TelemetryCollectorProtocol,
    TelemetryDatabaseWriter,
    TelemetrySampler,
)
from .alerts import AlertRuntime
from .telemetry import (
    DEFAULT_HOST_METRICS,
    ExporterBinding,
    GcLogBinding,
    LegacyTpsMode,
    ResourceRef,
    TelemetryCollector,
    TelemetryRuntime,
    TelemetryRuntimeConfig,
)

__all__ = [
    "AlertObservation",
    "AlertRuntime",
    "AlertSink",
    "DEFAULT_HOST_METRICS",
    "ExporterBinding",
    "GcLogBinding",
    "LegacyTpsMode",
    "ResourceRef",
    "StatusSnapshotProvider",
    "TelemetryCollector",
    "TelemetryCollectorProtocol",
    "TelemetryDatabaseWriter",
    "TelemetryRuntime",
    "TelemetryRuntimeConfig",
    "TelemetrySampler",
]
