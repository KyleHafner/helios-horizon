"""Typed, root-owned safety evidence for benchmark admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import sqlite3
import shutil
import time
from numbers import Real
from typing import Any, Callable, Mapping, Literal
import urllib.parse
import urllib.request

from .capability_evidence import WakeSafetyEvidence


EvidenceState = Literal["available", "inactive", "unavailable", "stale"]
_LEGACY_KEYS = (
    "maintenance_window", "storage_acceptable", "ups_acceptable",
    "quiet_period", "no_wake_session", "no_conflicting_jobs",
    "rollback_safe_public_wake",
)


@dataclass(frozen=True)
class EvidenceItem:
    check: str
    result: bool
    state: EvidenceState
    source: str
    observed_at: datetime
    reason: str = ""


@dataclass(frozen=True)
class BenchmarkEligibilityEvidence:
    items: tuple[EvidenceItem, ...]

    def legacy_mapping(self) -> dict[str, bool]:
        values = {item.check: item.result for item in self.items}
        return {key: values.get(key, False) for key in _LEGACY_KEYS}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def prometheus_ups_provider(config: Mapping[str, Any] | None) -> Callable[[], bool]:
    """Build a bounded, redirect-free UPS health probe."""
    settings = config if isinstance(config, Mapping) else {}
    origin = settings.get("url")
    metric = settings.get("metric")
    try:
        parsed = urllib.parse.urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not isinstance(origin, str)
            or len(origin) > 256
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
    except (ValueError, TypeError):
        return lambda: False
    if not isinstance(metric, str) or not metric or len(metric) > 128:
        return lambda: False
    endpoint = origin.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(metric, safe="")

    def check() -> bool:
        try:
            request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args: Any, **kwargs: Any):
                    return None

            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(request, timeout=1.0) as response:
                if response.geturl() != endpoint or response.status != 200:
                    return False
                body = response.read(128 * 1024 + 1)
            if len(body) > 128 * 1024:
                return False
            payload = json.loads(body)
            results = payload.get("data", {}).get("result", [])
            if payload.get("status") != "success" or not isinstance(results, list) or len(results) != 1:
                return False
            value = results[0].get("value")
            if not isinstance(value, list) or len(value) != 2:
                return False
            timestamp, state = float(value[0]), float(value[1])
            return math.isfinite(timestamp) and math.isfinite(state) and abs(time.time() - timestamp) <= 120 and state == 0.0
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    return check


class BenchmarkPreflight:
    """Evaluate benchmark safety without performing benchmark or DB writes."""

    def __init__(
        self,
        *,
        # Storage is safety evidence, so callers must opt into the paths they
        # have actually configured.  An unconfigured preflight must not turn
        # the host's incidental free space into benchmark authorization.
        storage_paths: tuple[str, ...] = (),
        storage_usage: Callable[[str], Any] = shutil.disk_usage,
        ups_health: Callable[[], Any] | None = None,
        session_store: Any | None = None,
        wake_evidence: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage_paths = tuple(storage_paths)
        self.storage_usage = storage_usage
        self.ups_health = ups_health
        self.session_store = session_store
        self.wake_evidence = wake_evidence
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _call(self, function: Callable[..., Any], *args: Any) -> Any:
        value = await asyncio.to_thread(function, *args)
        return await value if hasattr(value, "__await__") else value

    async def _storage(self, observed_at: datetime) -> EvidenceItem:
        if not self.storage_paths:
            return EvidenceItem("storage_acceptable", False, "unavailable", "root storage", observed_at, "storage paths are unavailable")
        results = await asyncio.gather(
            *(self._call(self.storage_usage, path) for path in self.storage_paths),
            return_exceptions=True,
        )
        def has_acceptable_free_space(result: Any) -> bool:
            try:
                free = result.free
                return (
                    isinstance(free, Real)
                    and not isinstance(free, bool)
                    and math.isfinite(free)
                    and free >= 5 * 1024**3
                )
            except Exception:
                return False

        ok = all(not isinstance(result, BaseException) and has_acceptable_free_space(result) for result in results)
        return EvidenceItem(
            "storage_acceptable", ok, "available" if ok else "unavailable",
            "root storage", observed_at, "" if ok else "storage free space is unavailable or below threshold",
        )

    async def _ups(self, observed_at: datetime) -> EvidenceItem:
        if self.ups_health is None:
            return EvidenceItem("ups_acceptable", False, "unavailable", "UPS provider", observed_at, "UPS evidence is unavailable")
        try:
            ok = (await self._call(self.ups_health)) is True
        except Exception:
            ok = False
        return EvidenceItem("ups_acceptable", ok, "available" if ok else "unavailable", "UPS provider", observed_at, "" if ok else "UPS is unavailable or unacceptable")

    def _sessions(self, observed_at: datetime) -> tuple[EvidenceItem, EvidenceItem]:
        connection = getattr(self.session_store, "connection", None)
        if connection is None:
            reason = "session state is unavailable"
            return (
                EvidenceItem("quiet_period", False, "unavailable", "root session state", observed_at, reason),
                EvidenceItem("no_wake_session", False, "unavailable", "root session state", observed_at, reason),
            )
        try:
            latest = connection.execute(
                "SELECT ended_at FROM player_sessions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1"
            ).fetchone()
            active = connection.execute("SELECT 1 FROM player_sessions WHERE ended_at IS NULL LIMIT 1").fetchone() is None
            quiet = latest is None or datetime.fromisoformat(latest[0].replace("Z", "+00:00")).timestamp() <= observed_at.timestamp() - 900
        except (AttributeError, IndexError, TypeError, ValueError, OSError, sqlite3.Error):
            reason = "session state is unreadable"
            return (
                EvidenceItem("quiet_period", False, "unavailable", "root session state", observed_at, reason),
                EvidenceItem("no_wake_session", False, "unavailable", "root session state", observed_at, reason),
            )
        return (
            EvidenceItem("quiet_period", quiet, "available" if quiet else "stale", "root session state", observed_at, "" if quiet else "quiet period has not elapsed"),
            EvidenceItem("no_wake_session", active, "available" if active else "inactive", "root session state", observed_at, "" if active else "active player session exists"),
        )

    async def evaluate(
        self,
        *,
        snapshot: Any,
        maintenance_window: bool,
        rollback_safe: bool,
        public_wake_policy: str,
    ) -> BenchmarkEligibilityEvidence:
        observed_at = _utc(self.clock())
        statuses = tuple(getattr(snapshot, "profiles", ()))
        stopped = all(
            getattr(item.state, "value", item.state) == "stopped"
            and item.players_online == 0
            and item.active_job_id is None
            for item in statuses
        )
        checks = [
            EvidenceItem("maintenance_window", maintenance_window, "available" if maintenance_window else "inactive", "schedule policy", observed_at),
            EvidenceItem("no_conflicting_jobs", stopped, "available" if stopped else "inactive", "fresh status snapshot", getattr(snapshot, "observed_at", observed_at), "" if stopped else "profile is not proven stopped and empty"),
            EvidenceItem("rollback_safe_public_wake", rollback_safe and public_wake_policy == "safe", "available" if rollback_safe and public_wake_policy == "safe" else "inactive", "schedule policy", observed_at),
        ]
        checks.append(await self._storage(observed_at))
        checks.append(await self._ups(observed_at))
        checks.extend(self._sessions(observed_at))
        wake = None
        if self.wake_evidence is not None:
            try:
                wake = await self._call(self.wake_evidence)
            except Exception:
                wake = None
        wake_ok = isinstance(wake, WakeSafetyEvidence) and wake.available is True and wake.clear is True
        checks.append(EvidenceItem("no_wake_session", False, "unavailable", "root wake evidence", observed_at, "root wake evidence is unavailable"))
        session_item = next(item for item in checks if item.check == "no_wake_session" and item.source == "root session state")
        checks[-1] = EvidenceItem(
            "no_wake_session", session_item.result and wake_ok,
            "available" if session_item.result and wake_ok else "unavailable",
            "root wake evidence", observed_at, "" if session_item.result and wake_ok else "root wake evidence is unavailable or not clear",
        )
        return BenchmarkEligibilityEvidence(tuple(checks))


__all__ = ["BenchmarkEligibilityEvidence", "BenchmarkPreflight", "EvidenceItem", "prometheus_ups_provider"]
