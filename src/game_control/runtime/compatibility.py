"""Compatibility bindings for pre-runtime telemetry callers.

The composition root imports these adapters by their historical private names;
collection and cleanup policy stays with the runtime compatibility boundary.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Mapping

from .protocols import AlertObservation
from .telemetry import (
    LegacyTpsMode,
    ResourceRef,
    TelemetryCollector,
    TelemetryRuntimeConfig,
)
from ..models import ProfileId


def _key(value: Any) -> str:
    value = getattr(value, "id", value)
    value = getattr(value, "value", value)
    return str(value)


def legacy_telemetry_config(stats: Mapping[str, Any], approved_profile_ids: tuple[str, ...]):
    """Translate the pre-runtime root section at this compatibility edge."""
    if not isinstance(stats, Mapping):
        raise ValueError("legacy telemetry settings are invalid")
    allowed = {
        "exporter_url", "tick_profile", "log_checkpoint_dir", "gc_log_path",
        "gc_profile_id", "host_metrics", "legacy_tps_mode",
        "legacy_tps_interval_seconds", "tps_interval_seconds",
    }
    if set(stats) - allowed:
        raise ValueError("legacy telemetry settings contain unknown keys")
    approved = tuple(str(getattr(item, "value", item)) for item in approved_profile_ids)
    settings: dict[str, Any] = {"legacy_tps_mode": stats.get("legacy_tps_mode", "disabled")}
    for key in ("log_checkpoint_dir", "host_metrics", "legacy_tps_interval_seconds"):
        if key in stats:
            settings[key] = stats[key]
    if "legacy_tps_interval_seconds" not in settings and "tps_interval_seconds" in stats:
        settings["legacy_tps_interval_seconds"] = stats["tps_interval_seconds"]
    if stats.get("exporter_url") is not None and str(stats.get("legacy_tps_mode", "disabled")) != LegacyTpsMode.ENABLED.value:
        tick_profile = stats.get("tick_profile")
        if tick_profile is None and ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value in approved:
            tick_profile = ProfileId.MINECRAFT_SUNLIT_COBBLEMON.value
        tick_profile = None if tick_profile is None else str(getattr(tick_profile, "value", tick_profile))
        if tick_profile is not None and tick_profile not in approved:
            raise ValueError("legacy tick profile is not in the approved root registry")
        if tick_profile is not None:
            settings.update(exporter_url=stats["exporter_url"], tick_profile=tick_profile)
    gc_profile, gc_path = stats.get("gc_profile_id"), stats.get("gc_log_path")
    if gc_profile is not None or gc_path is not None:
        settings.update(gc_profile_id=str(getattr(gc_profile, "value", gc_profile)), gc_log_path=gc_path)
    return TelemetryRuntimeConfig.from_root_config(settings, approved_profile_ids=approved)


class LegacyAlertSink:
    def __init__(self, target: Any):
        self.target = target

    def observe(self, observation: Any) -> None:
        if isinstance(observation, AlertObservation):
            self.target.observe(observation)


class BoundTelemetryCollectors(TelemetryCollector):
    """Bounded compatibility call-shape adapter for pre-composition callers."""

    def __init__(self, *, profiles: tuple[Any, ...], database: Any, stats: Mapping[str, Any], rcon: Any, player_tracker: Any, alerts: Any | None = None):
        self._legacy_database = database.value if isinstance(database, ResourceRef) else database
        self._legacy_rcon = rcon.value if isinstance(rcon, ResourceRef) else rcon
        self._legacy_rcon_ref = rcon if isinstance(rcon, ResourceRef) else (None if rcon is None else ResourceRef.borrowed(rcon))
        self._legacy_alerts = alerts
        super().__init__(profiles=profiles, config=legacy_telemetry_config(stats, tuple(_key(item) for item in profiles)), database=database, rcon=rcon, player_tracker=player_tracker, alert_sink=None if alerts is None else LegacyAlertSink(alerts))

    @property
    def alerts(self) -> Any | None:
        return self._legacy_alerts

    @alerts.setter
    def alerts(self, value: Any | None) -> None:
        self._legacy_alerts = value
        self.alert_sink = None if value is None else LegacyAlertSink(value)

    async def collect(self, *, running: Mapping[str, Any]) -> None:
        statuses = []
        for profile in self.profiles:
            value = running.get(_key(profile), False)
            statuses.append(value if not isinstance(value, bool) else type("LegacyStatus", (), {"profile_id": _key(profile), "state": "running" if value else "stopped", "pid": None, "rss_bytes": None})())
        await super().collect(type("LegacySnapshot", (), {"profiles": tuple(statuses)})())

    async def close(self) -> None:
        await self.aclose()
        drain = getattr(self._legacy_database, "drain", None)
        cleanup_error: BaseException | None = None
        cancelled = False
        if callable(drain):
            operation = asyncio.ensure_future(asyncio.to_thread(drain, 10.0))
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    cancelled = True
                    continue
            try:
                if operation.result() is not True:
                    cleanup_error = RuntimeError("legacy telemetry database drain failed")
            except BaseException as exc:
                cleanup_error = exc
        elif self._legacy_database is not None:
            cleanup_error = RuntimeError("legacy telemetry database drain is unavailable")
        if self._legacy_rcon_ref is not None and self._legacy_rcon_ref.owns_value:
            close = getattr(self._legacy_rcon, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            elif cleanup_error is None:
                cleanup_error = RuntimeError("legacy telemetry RCON close is unavailable")
        if cancelled:
            raise asyncio.CancelledError
        if cleanup_error is not None:
            raise cleanup_error


__all__ = ["BoundTelemetryCollectors", "LegacyAlertSink", "legacy_telemetry_config"]
