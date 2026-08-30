"""Bounded, stateful Prometheus tick telemetry.

This module deliberately keeps exporter parsing separate from scrape state.  A
Prometheus histogram is cumulative; treating its lifetime buckets as an
interval distribution is a subtle but serious correctness bug.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|Inf|-Inf)(?:\s+\S+)?$"
)
_LABEL = re.compile(r'\s*(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\[\\"n]|[^"\\])*)"\s*')
_IDENTITY_LABELS = frozenset({"player", "player_name", "player_uuid", "uuid", "username", "user"})

MAX_PROFILES = 16
MAX_HISTOGRAMS_PER_PROFILE = 8
MAX_BUCKETS = 64
MAX_LABELS = 8
MAX_LABEL_BYTES = 128


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]
    value: float


@dataclass(frozen=True)
class TickSnapshot:
    histogram: Mapping[str, tuple[tuple[float, float], ...]]
    sums: Mapping[str, float]
    counts: Mapping[str, float]
    summaries: Mapping[tuple[str, float], float]


@dataclass(frozen=True)
class TickWindow:
    state: str
    reason: str | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    p99_ms: float | None = None
    count: float | None = None
    histogram: tuple[tuple[float, float], ...] = ()


Parser = Callable[[str], TickSnapshot]


@dataclass(frozen=True)
class ExporterSpec:
    """One approved exporter endpoint and parser for a game profile."""

    profile_id: str
    url: str
    parser: Parser
    verified_summary_window: bool = False

    def __post_init__(self) -> None:
        if not _safe_key(self.profile_id):
            raise ValueError("invalid profile id")
        if not _safe_exporter_url(self.url):
            raise ValueError("invalid exporter URL")
        if not isinstance(self.verified_summary_window, bool):
            raise ValueError("invalid summary verification")


class ExporterRegistry:
    """Bounded profile registry; labels never contain player identity."""

    def __init__(self, specs: Mapping[str, ExporterSpec] | None = None) -> None:
        self._specs: dict[str, ExporterSpec] = {}
        for spec in (specs or {}).values():
            self.register(spec)

    def register(self, spec: ExporterSpec) -> None:
        if spec.profile_id not in self._specs and len(self._specs) >= MAX_PROFILES:
            raise ValueError("exporter profile limit exceeded")
        self._specs[spec.profile_id] = spec

    def get(self, profile_id: str) -> ExporterSpec:
        try:
            return self._specs[profile_id]
        except KeyError as exc:
            raise KeyError(f"unknown exporter profile: {profile_id}") from exc

    def profiles(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))


class PrometheusTickParser:
    """Parse one configured histogram/summary family from Prometheus text."""

    def __init__(self, metric: str = "mc_server_tick_seconds") -> None:
        if not _safe_key(metric):
            raise ValueError("invalid metric name")
        self.metric = metric

    def __call__(self, text: str) -> TickSnapshot:
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1_048_576:
            raise ValueError("invalid or oversized exporter response")
        buckets: dict[str, dict[float, float]] = {}
        sums: dict[str, float] = {}
        counts: dict[str, float] = {}
        summaries: dict[tuple[str, float], float] = {}
        seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = _SAMPLE.match(line)
            if match is None:
                if line.startswith(self.metric):
                    raise ValueError("malformed target sample")
                continue
            name = match.group("name")
            if name not in {
                self.metric,
                self.metric + "_bucket",
                self.metric + "_sum",
                self.metric + "_count",
            }:
                continue
            try:
                value = float(match.group("value"))
            except ValueError as exc:
                raise ValueError("invalid sample value") from exc
            if not math.isfinite(value):
                raise ValueError("non-finite sample")
            labels = _labels(match.group("labels") or "")
            key = (match.group("name"), labels)
            if key in seen:
                raise ValueError("duplicate Prometheus sample")
            seen.add(key)
            static = tuple((k, v) for k, v in labels if k != "le" and k != "quantile")
            family = _family(static)
            if name == self.metric + "_bucket":
                raw_le = dict(labels).get("le")
                if raw_le is None:
                    raise ValueError("histogram bucket lacks le")
                upper = math.inf if raw_le == "+Inf" else float(raw_le)
                if family not in buckets and len(buckets) >= MAX_HISTOGRAMS_PER_PROFILE:
                    raise ValueError("histogram family limit exceeded")
                if upper < 0 or len(buckets.setdefault(family, {})) >= MAX_BUCKETS:
                    raise ValueError("invalid or oversized bucket set")
                buckets[family][upper] = value
            elif name == self.metric + "_sum":
                sums[family] = value
            elif name == self.metric + "_count":
                counts[family] = value
            elif name == self.metric:
                quantile = dict(labels).get("quantile")
                if quantile is not None:
                    q = float(quantile)
                    if not 0 <= q <= 1:
                        raise ValueError("invalid summary quantile")
                    summaries[(family, q)] = value
        histogram = {family: tuple(sorted(values.items())) for family, values in buckets.items()}
        for family, values in histogram.items():
            if any(value < 0 for _, value in values) or any(
                values[index][1] < values[index - 1][1] for index in range(1, len(values))
            ):
                raise ValueError("invalid cumulative histogram")
            if values and math.isinf(values[-1][0]) and family in counts and values[-1][1] != counts[family]:
                raise ValueError("histogram count mismatch")
        return TickSnapshot(histogram=histogram, sums=sums, counts=counts, summaries=summaries)


class TickTelemetry:
    """Turn cumulative scrapes into independent p50/p95/p99 windows."""

    def __init__(self, registry: ExporterRegistry) -> None:
        self.registry = registry
        self._previous: dict[str, TickSnapshot] = {}

    def scrape(self, profile_id: str, text: str) -> TickWindow:
        spec = self.registry.get(profile_id)
        try:
            current = spec.parser(text)
        except (TypeError, ValueError):
            self._previous.pop(profile_id, None)
            return TickWindow("unavailable", "invalid_scrape")
        previous = self._previous.get(profile_id)
        self._previous[profile_id] = current
        if previous is None:
            return TickWindow("unavailable", "warmup")
        if (
            set(current.histogram) != set(previous.histogram)
            or set(current.sums) != set(previous.sums)
            or set(current.counts) != set(previous.counts)
        ):
            return TickWindow("reset", "boundary_changed")
        windows: list[tuple[tuple[float, float], ...], float] = []
        for family, current_buckets in current.histogram.items():
            old_buckets = dict(previous.histogram[family])
            now_buckets = dict(current_buckets)
            if set(old_buckets) != set(now_buckets):
                return TickWindow("reset", "boundary_changed")
            if not math.isinf(current_buckets[-1][0]):
                return TickWindow("unavailable", "missing_inf_bucket")
            if any(
                current_buckets[index][1] < current_buckets[index - 1][1]
                for index in range(1, len(current_buckets))
            ):
                return TickWindow("unavailable", "invalid_bucket_counts")
            if family not in current.sums or family not in current.counts:
                return TickWindow("unavailable", "missing_histogram_total")
            if family not in previous.sums or family not in previous.counts:
                return TickWindow("reset", "boundary_changed")
            if any(now_buckets[k] < old_buckets[k] for k in now_buckets):
                return TickWindow("reset", "counter_regression")
            delta_sum = current.sums[family] - previous.sums[family]
            delta_count = current.counts[family] - previous.counts[family]
            if delta_sum < 0 or delta_count < 0:
                return TickWindow("reset", "counter_regression")
            deltas = tuple((bound, now_buckets[bound] - old_buckets[bound]) for bound in now_buckets)
            if any(value < 0 for _, value in deltas):
                return TickWindow("reset", "counter_regression")
            if delta_count <= 0 or not math.isfinite(delta_sum):
                return TickWindow("unavailable", "empty_window")
            if not math.isclose(deltas[-1][1], delta_count, rel_tol=0.0, abs_tol=1e-9):
                return TickWindow("unavailable", "histogram_count_mismatch")
            if any(count > delta_count for _, count in deltas):
                return TickWindow("unavailable", "invalid_bucket_counts")
            windows.append((deltas, delta_count))
        if len(windows) != 1:
            return TickWindow("unavailable", "ambiguous_histograms")
        deltas, count = windows[0]
        return TickWindow(
            "available",
            p50_ms=_histogram_quantile(0.50, deltas, count) * 1000,
            p95_ms=_histogram_quantile(0.95, deltas, count) * 1000,
            p99_ms=_histogram_quantile(0.99, deltas, count) * 1000,
            count=count,
            histogram=deltas,
        )

    def verified_summary_window(self, profile_id: str, text: str) -> TickWindow:
        """Use summary quantiles only when the exporter declares a window."""
        spec = self.registry.get(profile_id)
        if not spec.verified_summary_window:
            return TickWindow("unavailable", "summary_window_unverified")
        try:
            snapshot = spec.parser(text)
        except (TypeError, ValueError):
            return TickWindow("unavailable", "invalid_scrape")
        families = {family for family, _ in snapshot.summaries}
        if len(families) != 1:
            return TickWindow("unavailable", "summary_window_unverified")
        family = next(iter(families))
        values = [snapshot.summaries.get((family, q)) for q in (0.5, 0.95, 0.99)]
        if any(value is None for value in values):
            return TickWindow("unavailable", "summary_window_unverified")
        return TickWindow("available", p50_ms=values[0] * 1000, p95_ms=values[1] * 1000, p99_ms=values[2] * 1000)


def _safe_key(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_:.-]{0,127}", value))


def _safe_exporter_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 512:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/metrics"
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


def _labels(raw: str) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    labels = []
    position = 0
    while position < len(raw):
        match = _LABEL.match(raw, position)
        if match is None:
            raise ValueError("invalid labels")
        labels.append(match)
        position = match.end()
        if position == len(raw):
            break
        if raw[position] != ",":
            raise ValueError("invalid labels")
        position += 1
        if position == len(raw):
            raise ValueError("invalid labels")
    if len(labels) > MAX_LABELS or sum(len(match.group("value")) for match in labels) > MAX_LABEL_BYTES:
        raise ValueError("labels exceed bounds")
    parsed = []
    for match in labels:
        key, value = match.group("key"), match.group("value")
        if key.lower() in _IDENTITY_LABELS:
            raise ValueError("player identity label is not allowed")
        parsed.append((key, value.replace(r"\n", "\n").replace(r'\"', '"').replace(r"\\", "\\")))
    if len({key for key, _ in parsed}) != len(parsed):
        raise ValueError("duplicate label")
    return tuple(sorted(parsed))


def _family(labels: tuple[tuple[str, str], ...]) -> str:
    return "&".join(f"{key}={value}" for key, value in labels)


def _histogram_quantile(q: float, buckets: tuple[tuple[float, float], ...], count: float) -> float:
    if not buckets or not math.isinf(buckets[-1][0]) or count <= 0:
        raise ValueError("histogram requires +Inf bucket")
    rank = q * count
    previous_bound = 0.0
    previous_count = 0.0
    for upper, cumulative in buckets:
        if cumulative >= rank:
            if math.isinf(upper):
                return previous_bound
            span = cumulative - previous_count
            return upper if span <= 0 else previous_bound + (upper - previous_bound) * (rank - previous_count) / span
        previous_bound, previous_count = upper, cumulative
    return previous_bound


__all__ = [
    "ExporterRegistry", "ExporterSpec", "MAX_BUCKETS", "MAX_HISTOGRAMS_PER_PROFILE", "MAX_LABELS", "MAX_PROFILES", "PrometheusTickParser",
    "TickSnapshot", "TickTelemetry", "TickWindow",
]
