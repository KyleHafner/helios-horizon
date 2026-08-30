"""Frozen root configuration and source contracts for telemetry runtime work.

This commit intentionally defines no collector loops and performs no wiring.
The later runtime extraction consumes these records without accepting values
from RPC or browser requests.
"""

from __future__ import annotations

import asyncio
import math
import time
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Generic, Iterable, Mapping, TypeVar
from urllib.parse import urlsplit

from .protocols import (
    AlertObservation,
    AlertSink,
    StatusSnapshotProvider,
    TelemetryCollector as TelemetryCollectorProtocol,
    TelemetryDatabaseWriter,
    TelemetrySampler,
    _is_safe_identifier,
)
from ..gc_telemetry import GcTelemetryParser
from ..log_follower import LogFollower
from ..metrics import HostTelemetrySource
from ..rcon_telemetry import PerformanceResult, PersistentRconTelemetry, PlayerCountResult, TelemetryCommand
from ..tick_telemetry import ExporterRegistry, ExporterSpec, PrometheusTickParser, TickTelemetry
from ..players import PlayerTracker
from ..telemetry_sampler import TelemetrySampler as Scheduler


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
        "gc_profile_id",
        "host_metrics",
        "legacy_tps_mode",
        "legacy_tps_interval_seconds",
    }
)


class LegacyTpsMode(StrEnum):
    """Explicit compatibility selection for legacy state-db TPS sampling."""

    DISABLED = "disabled"
    ENABLED = "enabled"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ResourceRef(Generic[T]):
    """An explicit borrowed/owned resource marker."""

    value: T
    owns_value: bool = False

    @classmethod
    def borrowed(cls, value: T) -> "ResourceRef[T]":
        return cls(value, False)

    @classmethod
    def owned(cls, value: T) -> "ResourceRef[T]":
        return cls(value, True)


@dataclass(frozen=True, slots=True)
class GcLogBinding:
    """One root-approved profile and its GC log path."""

    profile_id: str
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_id", _safe_identifier(self.profile_id, name="GC profile"))
        object.__setattr__(self, "path", _safe_path(self.path, name="GC log path"))


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
    gc_log: GcLogBinding | None = None
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
        if self.gc_log is not None and not isinstance(self.gc_log, GcLogBinding):
            raise ValueError("GC log binding is invalid")

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

        gc_profile_id = settings.get("gc_profile_id")
        gc_log_path = settings.get("gc_log_path")
        if (gc_profile_id is None) != (gc_log_path is None):
            raise ValueError("GC profile and path must be configured together")
        gc_log = None
        if gc_profile_id is not None:
            profile_id = _safe_identifier(gc_profile_id, name="GC profile")
            if profile_id not in approved:
                raise ValueError("GC profile is not in the approved root registry")
            gc_log = GcLogBinding(profile_id, _safe_path(gc_log_path, name="GC log path"))

        raw_metrics = settings.get("host_metrics", DEFAULT_HOST_METRICS)
        if not isinstance(raw_metrics, (list, tuple)):
            raise ValueError("host metric registry is invalid")
        return cls(
            exporters=exporters,
            log_checkpoint_dir=None if settings.get("log_checkpoint_dir") is None else _safe_path(settings["log_checkpoint_dir"], name="log checkpoint directory"),
            gc_log=gc_log,
            host_metrics=tuple(raw_metrics),
            legacy_tps_mode=settings["legacy_tps_mode"],
            legacy_tps_interval_seconds=settings.get("legacy_tps_interval_seconds", 30.0),
        )


def _key(value: Any) -> str:
    value = getattr(value, "id", value)
    value = getattr(value, "value", value)
    return str(value)


def _wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _resource(value: Any) -> ResourceRef[Any] | None:
    if value is None:
        return None
    if isinstance(value, ResourceRef):
        return value
    return ResourceRef.borrowed(value)


async def _await_cleanup(awaitable: Awaitable[Any]) -> bool:
    """Drain one cleanup operation even when its caller is cancelled."""

    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                if cancelled:
                    return True
                raise
            cancelled = True
            continue
        break
    return cancelled


class TelemetryCollector:
    """Collect generic telemetry for one immutable status snapshot."""

    def __init__(
        self,
        *,
        profiles: tuple[Any, ...],
        config: TelemetryRuntimeConfig,
        database: ResourceRef[TelemetryDatabaseWriter] | TelemetryDatabaseWriter | None,
        rcon: ResourceRef[PersistentRconTelemetry] | PersistentRconTelemetry | None,
        player_tracker: PlayerTracker,
        alert_sink: AlertSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] = _wall_clock_ms,
        fetch_exporter: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.profiles = tuple(profiles)
        self.config = config
        self._database_ref = _resource(database)
        self._rcon_ref = _resource(rcon)
        self.player_tracker = player_tracker
        self.alert_sink = alert_sink
        self._monotonic = monotonic
        self._wall_clock_ms = wall_clock_ms
        self._fetch_exporter = fetch_exporter
        self._last_tick = 0.0
        self._tick: TickTelemetry | None = None
        self._tick_profile: str | None = None
        self._followers: dict[str, LogFollower] = {}
        self._gc_parser: GcTelemetryParser | None = None
        self._gc_profile: str | None = None
        self._tick_buckets: tuple[float, ...] = ()
        self._host_state: dict[str, str] = {}
        self._last_active_profile: str | None = None
        self._rcon_was_active = False
        self._failures: dict[str, int] = {}
        self._host = HostTelemetrySource()
        self._closed = False

        specs = {
            binding.profile_id: ExporterSpec(binding.profile_id, binding.url, PrometheusTickParser())
            for binding in config.exporters
        }
        if specs:
            self._tick = TickTelemetry(ExporterRegistry(specs))
            self._tick_profile = next(iter(specs))
        if config.log_checkpoint_dir is not None:
            root = config.log_checkpoint_dir
            for profile in self.profiles:
                logs = getattr(getattr(profile, "paths", None), "log_files", ())
                if logs:
                    profile_id = _key(profile)
                    self.player_tracker.register_incremental(profile_id)
                    self._followers[profile_id] = LogFollower(
                        logs[0], root / f"{profile_id}.json", start_at_end=True
                    )
        if config.gc_log is not None:
            self._gc_profile = config.gc_log.profile_id
            self._gc_parser = GcTelemetryParser()
            self._followers[f"{self._gc_profile}:gc"] = LogFollower(
                config.gc_log.path,
                config.log_checkpoint_dir / f"{self._gc_profile}-gc.json"
                if config.log_checkpoint_dir is not None else None,
                start_at_end=True,
            )

    @property
    def database(self) -> TelemetryDatabaseWriter | None:
        return None if self._database_ref is None else self._database_ref.value

    @property
    def rcon(self) -> PersistentRconTelemetry | None:
        return None if self._rcon_ref is None else self._rcon_ref.value

    async def collect(self, snapshot: Any) -> None:
        if self._closed:
            return
        statuses = tuple(getattr(snapshot, "profiles", ()))
        running: dict[str, Any] = {
            _key(getattr(status, "profile_id", status)): status for status in statuses
        }

        def is_running(profile_id: str) -> bool:
            value = running.get(profile_id, False)
            if isinstance(value, bool):
                return value
            state = getattr(getattr(value, "state", None), "value", getattr(value, "state", None))
            return state == "running"

        now = self._monotonic()
        tick_running = is_running(self._tick_profile or "")
        tick_p95 = None
        if self._tick is not None and self._tick_profile is not None and now - self._last_tick >= self.config.tick_interval_seconds:
            self._last_tick = now
            tick_p95 = await self._collect_tick(self._tick_profile, tick_running)
        if self.rcon is not None:
            rcon_running = is_running(self.rcon.profile_id)
            await self.rcon.set_active(rcon_running)
            if rcon_running:
                await self._collect_rcon()
                self._rcon_was_active = True
            elif self._rcon_was_active:
                stamp = self._wall_clock_ms()
                for metric in ("players", "tps", "mspt"):
                    self._record(self.rcon.profile_id, metric, None, "inactive", labels={"source": "rcon"}, ts_ms=stamp)
                self._rcon_was_active = False
        for follower_id, follower in self._followers.items():
            try:
                if follower_id.endswith(":gc") and self._gc_profile is not None and not is_running(self._gc_profile):
                    self._record(self._gc_profile, "gc_pause", None, "inactive")
                    continue
                if follower_id.endswith(":gc") and self._gc_parser is not None:
                    async def gc_callback(event: Any) -> None:
                        if event.kind == "reset":
                            self._gc_parser.reset()
                            return
                        for gc_event in self._gc_parser.feed((str(event.line or "") + "\n").encode()):
                            if gc_event.duration_ms is not None and self._gc_profile is not None:
                                self._record(self._gc_profile, "gc_pause", gc_event.duration_ms, "available")
                    await follower.follow_async(gc_callback)
                else:
                    await follower.follow_async(lambda event, pid=follower_id: self.player_tracker.ingest_event(pid, event))
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures[f"log:{follower_id}"] = self._failures.get(f"log:{follower_id}", 0) + 1

        active = next((profile for profile in self.profiles if is_running(_key(profile))), None)
        active_id = None if active is None else _key(active)
        inactive_targets = {item for item in (self._last_active_profile, self._tick_profile, None if self.rcon is None else self.rcon.profile_id) if item}
        for profile_id in inactive_targets - ({active_id} if active_id else set()):
            if self._host_state.get(profile_id) != "inactive":
                for metric in self.config.host_metrics:
                    self._record(profile_id, metric, None, "inactive")
                self._host_state[profile_id] = "inactive"
        if active is not None:
            status = running.get(active_id)
            try:
                pid = None if isinstance(status, bool) else getattr(status, "pid", None)
                values = self._host.collect(active, pid=pid)
                for metric in self.config.host_metrics:
                    value = values.get(metric)
                    self._record(active_id, metric, value, "available" if value is not None else "unavailable")
                self._host_state[active_id] = "available"
                self._last_active_profile = active_id
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures["host"] = self._failures.get("host", 0) + 1
                for metric in self.config.host_metrics:
                    self._record(active_id, metric, None, "unavailable")
                self._host_state[active_id] = "unavailable"
            if self.alert_sink is not None:
                self.alert_sink.observe(AlertObservation(
                    profile_id=active_id,
                    profile_state="running",
                    now=now,
                    mspt_p95=tick_p95 if active_id == self._tick_profile else None,
                    rss_bytes=None if isinstance(status, bool) else getattr(status, "rss_bytes", None),
                ))
        elif self.alert_sink is not None and self._last_active_profile is not None:
            self.alert_sink.observe(AlertObservation(
                profile_id=self._last_active_profile, profile_state="stopped", now=now,
            ))

    async def _collect_tick(self, profile_id: str, running: bool) -> float | None:
        if not running:
            for metric in ("mspt", "mspt_p50", "mspt_p95", "mspt_p99"):
                self._record(profile_id, metric, None, "inactive")
            for bucket in self._tick_buckets:
                self._record(profile_id, "tick_ms_bucket", None, "inactive", labels={"bucket": "+Inf" if bucket == float("inf") else str(bucket), "source": "prometheus"})
            return None
        try:
            async def fetch() -> str:
                if self._fetch_exporter is not None:
                    return await self._fetch_exporter(self._tick.registry.get(profile_id).url)
                def read() -> str:
                    with urllib.request.urlopen(self._tick.registry.get(profile_id).url, timeout=2.0) as response:
                        raw = response.read(1_048_577)
                        if len(raw) > 1_048_576:
                            raise ValueError("exporter response exceeded bound")
                        return raw.decode("utf-8", "replace")
                return await asyncio.to_thread(read)
            window = self._tick.scrape(profile_id, await fetch())
            if window.state == "available":
                for metric, value in (("mspt_p50", window.p50_ms), ("mspt_p95", window.p95_ms), ("mspt_p99", window.p99_ms)):
                    self._record(profile_id, metric, value, "available")
                for bucket, count in window.histogram:
                    self._record(profile_id, "tick_ms_bucket", count, "available", labels={"bucket": "+Inf" if bucket == float("inf") else str(bucket), "source": "prometheus"})
                self._tick_buckets = tuple(bucket for bucket, _count in window.histogram)
                return window.p95_ms
            for metric in ("mspt_p50", "mspt_p95", "mspt_p99"):
                self._record(profile_id, metric, None, "unavailable")
            for bucket in self._tick_buckets:
                self._record(profile_id, "tick_ms_bucket", None, "unavailable", labels={"bucket": "+Inf" if bucket == float("inf") else str(bucket), "source": "prometheus"})
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failures["tick"] = self._failures.get("tick", 0) + 1
            for metric in ("mspt_p50", "mspt_p95", "mspt_p99"):
                self._record(profile_id, metric, None, "unavailable")
            for bucket in self._tick_buckets:
                self._record(profile_id, "tick_ms_bucket", None, "unavailable", labels={"bucket": "+Inf" if bucket == float("inf") else str(bucket), "source": "prometheus"})
        return None

    async def _collect_rcon(self) -> None:
        assert self.rcon is not None
        stamp = self._wall_clock_ms()
        try:
            players = await self.rcon.execute(TelemetryCommand.PLAYER_COUNT)
            if not isinstance(players, PlayerCountResult):
                raise ValueError("typed player-count result unavailable")
            self._record(self.rcon.profile_id, "players", players.online, "available", labels={"source": "rcon"}, ts_ms=stamp)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failures["rcon:players"] = self._failures.get("rcon:players", 0) + 1
            self._record(self.rcon.profile_id, "players", None, "unavailable", labels={"source": "rcon"}, ts_ms=stamp)
        try:
            performance = await self.rcon.execute(TelemetryCommand.PERFORMANCE)
            if not isinstance(performance, PerformanceResult):
                raise ValueError("typed performance result unavailable")
            self._record(self.rcon.profile_id, "tps", performance.tps, "available", labels={"source": "rcon"}, ts_ms=stamp)
            self._record(self.rcon.profile_id, "mspt", performance.mspt, "available", labels={"source": "rcon"}, ts_ms=stamp)
        except asyncio.CancelledError:
            raise
        except Exception:
            for metric in ("tps", "mspt"):
                self._failures[f"rcon:{metric}"] = self._failures.get(f"rcon:{metric}", 0) + 1
                self._record(self.rcon.profile_id, metric, None, "unavailable", labels={"source": "rcon"}, ts_ms=stamp)

    def _record(self, profile_id: str, metric: str, value: Any, state: str, *, labels: Mapping[str, str] | None = None, ts_ms: int | None = None) -> None:
        if self.database is None:
            return
        try:
            self.database.enqueue_sample(profile_id, metric, value, ts_ms=self._wall_clock_ms() if ts_ms is None else ts_ms, state=state, labels=labels)
        except Exception:
            self._failures[f"db:{metric}"] = self._failures.get(f"db:{metric}", 0) + 1

    def health(self) -> Mapping[str, Any]:
        rcon = None if self.rcon is None else self.rcon.health
        return {"failures": dict(self._failures), "rcon": None if rcon is None else rcon.__dict__, "closed": self._closed}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cancelled = False
        if self._rcon_ref is not None and self._rcon_ref.owns_value:
            close = getattr(self.rcon, "close", None)
            if callable(close):
                cancelled = (await _await_cleanup(close())) or cancelled
        if self._database_ref is not None:
            drain = getattr(self.database, "drain", None)
            if callable(drain):
                cancelled = (await _await_cleanup(asyncio.to_thread(drain, 10.0))) or cancelled
            if self._database_ref.owns_value:
                close = getattr(self.database, "close", None)
                if callable(close):
                    cancelled = (await _await_cleanup(asyncio.to_thread(close))) or cancelled
        if cancelled:
            raise asyncio.CancelledError


class TelemetryRuntime:
    """Own one cadence callback and one in-flight snapshot/cycle gate."""

    def __init__(self, status: StatusSnapshotProvider, collector: TelemetryCollectorProtocol, *, sampler: ResourceRef[TelemetrySampler] | TelemetrySampler | None = None, interval_seconds: float = 5.0) -> None:
        self.status = status
        self.collector = collector
        sampler_value = _resource(sampler)
        if sampler_value is None:
            sampler_value = ResourceRef.owned(Scheduler(self.sample_once, interval_seconds=interval_seconds))
        self._sampler_ref = sampler_value
        self._inflight: set[asyncio.Task[Any]] = set()
        self._cycle_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

    @property
    def sampler(self) -> TelemetrySampler:
        return self._sampler_ref.value

    def callback(self) -> Callable[[], Awaitable[None]]:
        return self.sample_once

    async def cycle(self, snapshot: Any) -> None:
        if self._closing:
            return
        async with self._cycle_lock:
            if self._closing:
                return
            await self.collector.collect(snapshot)

    async def _drain(self, task: asyncio.Task[Any]) -> Any:
        try:
            return await task
        except BaseException:
            raise

    async def sample_once(self) -> None:
        if self._closing:
            return
        status_task = asyncio.create_task(self.status.snapshot(persist=True, force=True), name="horizon-status-telemetry")
        self._inflight.add(status_task)
        try:
            try:
                snapshot = await asyncio.shield(status_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(status_task)
                except BaseException:
                    pass
                raise
        finally:
            self._inflight.discard(status_task)
        if self._closing:
            return
        cycle_task = asyncio.create_task(self.cycle(snapshot), name="horizon-telemetry-cycle")
        self._inflight.add(cycle_task)
        try:
            try:
                await asyncio.shield(cycle_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(cycle_task)
                except BaseException:
                    pass
                raise
        finally:
            self._inflight.discard(cycle_task)

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        cancelled = False
        sampler = self.sampler
        if self._sampler_ref.owns_value:
            cancelled = (await _await_cleanup(sampler.shutdown())) or cancelled
            cancelled = (await _await_cleanup(sampler.wait_closed())) or cancelled
        pending = tuple(self._inflight)
        if pending:
            drain = asyncio.gather(*pending, return_exceptions=True)
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                await asyncio.shield(drain)
                cancelled = True
        cancelled = (await _await_cleanup(self.collector.close())) or cancelled
        self._closed = True
        if cancelled:
            raise asyncio.CancelledError

    def health(self) -> Mapping[str, Any]:
        result = {"closed": self._closed, "closing": self._closing, "sampler": self.sampler.health() if hasattr(self.sampler, "health") else {}}
        result["collector"] = dict(self.collector.health())
        return result


__all__ = [
    "AlertSink",
    "DEFAULT_HOST_METRICS",
    "ExporterBinding",
    "GcLogBinding",
    "LegacyTpsMode",
    "ResourceRef",
    "StatusSnapshotProvider",
    "TelemetryCollectorProtocol",
    "TelemetryCollector",
    "TelemetryDatabaseWriter",
    "TelemetryRuntimeConfig",
    "TelemetrySampler",
]
