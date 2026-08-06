"""Minecraft Prometheus exporter parsing and low-rate TPS sampling."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

_LOG = logging.getLogger(__name__)
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|Inf|-Inf)\s*$"
)
_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"])*)"')
FIXED_EXPORTER_URL = "http://127.0.0.1:19565/metrics"
MAX_EXPORTER_RESPONSE_BYTES = 1024 * 1024
TPS_STALE_AFTER_SECONDS = 120
_TICK_PROFILES = frozenset({"minecraft", "minecraft-sunlit-cobblemon"})


def parse_metrics(text: str) -> tuple[float, float] | None:
    """Return capped TPS and MSPT from exporter summary or histogram output."""
    if not isinstance(text, str) or not text:
        return None
    summary: float | None = None
    histogram_sum: float | None = None
    histogram_count: float | None = None
    buckets: list[tuple[float, float]] = []
    for raw_line in text.splitlines():
        match = _SAMPLE.match(raw_line.strip())
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if not value == value or value in (float("inf"), float("-inf")):
            continue
        name = match.group("name")
        labels = {item.group("key"): item.group("value") for item in _LABEL.finditer(match.group("labels") or "")}
        if name in {"mc_tick_seconds", "mc_server_tick_seconds"} and labels.get("quantile") == "0.5":
            summary = value
        elif name == "mc_server_tick_seconds_sum":
            histogram_sum = value
        elif name == "mc_server_tick_seconds_count":
            histogram_count = value
        elif name == "mc_server_tick_seconds_bucket" and labels.get("le") not in (None, "+Inf"):
            try:
                buckets.append((float(labels["le"]), value))
            except ValueError:
                continue
    tick_seconds = summary
    if tick_seconds is None and histogram_sum is not None and histogram_count and histogram_count > 0:
        tick_seconds = histogram_sum / histogram_count
    if tick_seconds is None and buckets:
        total = max(count for _, count in buckets)
        midpoint = total / 2.0
        for upper_bound, count in sorted(buckets):
            if count >= midpoint:
                tick_seconds = upper_bound
                break
    if tick_seconds is None or tick_seconds <= 0:
        return None
    mspt = tick_seconds * 1000.0
    tps = min(20.0, 1.0 / tick_seconds)
    return tps, mspt


class TpsSampler:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        url: str = FIXED_EXPORTER_URL,
        profile_id: str = "minecraft",
        interval_seconds: float = 30.0,
        backoff_seconds: float = 300.0,
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if profile_id not in _TICK_PROFILES:
            raise ValueError("Minecraft is the only profile with tick telemetry")
        if url != FIXED_EXPORTER_URL:
            raise ValueError("tick telemetry source is not approved")
        self.connection = connection
        self.url = url
        self.profile_id = profile_id
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.backoff_seconds = max(self.interval_seconds, float(backoff_seconds))
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.consecutive_failures = 0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def run_once(self, *, now: str | None = None) -> bool:
        try:
            async with self._client.stream("GET", self.url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    if not isinstance(chunk, bytes):
                        raise TypeError("exporter response chunk was not bytes")
                    total += len(chunk)
                    if total > MAX_EXPORTER_RESPONSE_BYTES:
                        raise ValueError("exporter response exceeded size limit")
                    chunks.append(chunk)
            parsed = parse_metrics(b"".join(chunks).decode("utf-8"))
            if parsed is None:
                raise ValueError("exporter metrics were not parseable")
            timestamp = now or _iso(self._clock())
            with self.connection:
                self.connection.executemany(
                    "INSERT INTO metric_samples(profile_id, metric, ts, value) VALUES (?, ?, ?, ?)",
                    ((self.profile_id, "tps", timestamp, parsed[0]), (self.profile_id, "mspt", timestamp, parsed[1])),
                )
            return True
        except (asyncio.TimeoutError, httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            _LOG.debug("Minecraft TPS scrape skipped: %s", type(exc).__name__)
            return False

    async def run(
        self,
        is_running: Callable[[], bool | Awaitable[bool]],
    ) -> None:
        try:
            while True:
                try:
                    running = is_running()
                    if inspect.isawaitable(running):
                        running = await running
                except Exception:
                    running = False
                if running:
                    success = await self.run_once()
                    if success:
                        self.consecutive_failures = 0
                    else:
                        self.consecutive_failures += 1
                else:
                    self.consecutive_failures = 0
                delay = self.backoff_seconds if self.consecutive_failures >= 3 else self.interval_seconds
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "FIXED_EXPORTER_URL",
    "MAX_EXPORTER_RESPONSE_BYTES",
    "TPS_STALE_AFTER_SECONDS",
    "TpsSampler",
    "parse_metrics",
]
