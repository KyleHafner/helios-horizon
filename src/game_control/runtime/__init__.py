"""Typed seams for the slotd runtime extraction.

The runtime package intentionally contains contracts and small policy-free
records.  Construction and dependency selection remain in the composition
root; domain services do not import this package's future container.
"""

from .protocols import (
    AlertObservation,
    AlertSink,
    StatusSnapshotProvider,
    TelemetryCollector,
    TelemetryDatabaseWriter,
    TelemetrySampler,
)
from .telemetry import (
    DEFAULT_HOST_METRICS,
    ExporterBinding,
    LegacyTpsMode,
    TelemetryRuntimeConfig,
)

__all__ = [
    "AlertObservation",
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
