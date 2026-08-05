"""Read-only SQL-backed aggregations for Horizon player and TPS stats."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any


_PROFILE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "minecraft": {"player_tracking": "names", "occupancy": True, "tick_telemetry": True},
    "terraria-vanilla": {"player_tracking": "names", "occupancy": True, "tick_telemetry": False},
    "terraria-tmod": {"player_tracking": "names", "occupancy": True, "tick_telemetry": False},
    "pz-rising": {"player_tracking": "count", "occupancy": True, "tick_telemetry": False},
}


def stats_profile_capabilities(profile_id: str) -> dict[str, Any]:
    """Return the closed, UI-facing stat capabilities for one profile."""
    capabilities = _PROFILE_CAPABILITIES.get(str(profile_id))
    if capabilities is None:
        return {"player_tracking": "unavailable", "occupancy": False, "tick_telemetry": False}
    return dict(capabilities)


def stats_summary(
    connection: sqlite3.Connection,
    profile_id: str,
    days: int | None,
    *,
    now: str,
) -> dict[str, Any]:
    current = _parse(now)
    cutoff = current - timedelta(days=days) if days is not None else None
    rows = _session_rows(connection, profile_id, cutoff)
    by_player: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"hours": 0.0, "sessions": 0, "last_seen": None}
    )
    total_hours = 0.0
    for row in rows:
        interval = _interval(row, cutoff, current)
        if interval is None:
            continue
        started, ended = interval
        hours = (ended - started).total_seconds() / 3600.0
        item = by_player[row[2]]
        item["hours"] += hours
        item["sessions"] += 1
        seen = min(_parse(row[4]) if row[4] is not None else current, current)
        if item["last_seen"] is None or seen > item["last_seen"]:
            item["last_seen"] = seen
        total_hours += hours
    leaderboard = [
        {
            "player": player,
            "hours": round(item["hours"], 4),
            "sessions": item["sessions"],
            "last_seen": _iso(item["last_seen"]),
        }
        for player, item in sorted(
            by_player.items(),
            key=lambda pair: (-pair[1]["hours"], pair[0]),
        )[:20]
    ]
    return {
        "total_hours": round(total_hours, 4),
        "unique_players": len(by_player),
        "leaderboard": leaderboard,
        "player_tracking": stats_profile_capabilities(profile_id)["player_tracking"],
        "occupancy": _occupancy(connection, profile_id, cutoff),
    }


def _occupancy(
    connection: sqlite3.Connection, profile_id: str, cutoff: datetime | None
) -> dict[str, Any] | None:
    if not stats_profile_capabilities(profile_id)["occupancy"]:
        return None
    query = "SELECT ts, value FROM metric_samples WHERE profile_id=? AND metric='players'"
    params: tuple[Any, ...] = (profile_id,)
    if cutoff is not None:
        query += " AND ts >= ?"
        params += (_iso(cutoff),)
    rows = connection.execute(query + " ORDER BY ts", params).fetchall()
    samples = [{"ts": str(timestamp), "count": int(value)} for timestamp, value in rows]
    return {"latest": samples[-1]["count"] if samples else None, "samples": samples[-500:]}


def stats_heatmap(
    connection: sqlite3.Connection,
    profile_id: str,
    days: int,
    *,
    now: str,
) -> dict[str, Any]:
    current = _parse(now)
    cutoff = current - timedelta(days=days)
    buckets = [[0.0 for _ in range(24)] for _ in range(7)]
    for row in _session_rows(connection, profile_id, cutoff):
        interval = _interval(row, cutoff, current)
        if interval is None:
            continue
        started, ended = interval
        cursor = started.replace(minute=0, second=0, microsecond=0)
        while cursor < ended:
            next_hour = cursor + timedelta(hours=1)
            overlap = max(0.0, (min(next_hour, ended) - max(cursor, started)).total_seconds())
            buckets[cursor.weekday()][cursor.hour] += overlap / 3600.0
            cursor = next_hour
    return {"days": days, "buckets": buckets}


def stats_tps(
    connection: sqlite3.Connection,
    profile_id: str,
    window: str,
    *,
    now: str,
) -> dict[str, Any]:
    current = _parse(now)
    cutoff = current - timedelta(hours={"1h": 1, "6h": 6, "24h": 24}[window])
    rows = connection.execute(
        "SELECT ts, metric, value FROM metric_samples "
        "WHERE profile_id=? AND metric IN ('tps', 'mspt') AND ts >= ? "
        "ORDER BY ts",
        (profile_id, _iso(cutoff)),
    ).fetchall()
    paired: dict[str, dict[str, float]] = defaultdict(dict)
    for timestamp, metric, value in rows:
        paired[str(timestamp)][str(metric)] = float(value)
    samples = [
        {"ts": timestamp, "tps": values["tps"], "mspt": values["mspt"]}
        for timestamp, values in sorted(paired.items())
        if "tps" in values and "mspt" in values
    ]
    return {"window": window, "samples": _downsample(samples)}


def _session_rows(
    connection: sqlite3.Connection, profile_id: str, cutoff: datetime | None
) -> list[tuple[Any, ...]]:
    if cutoff is None:
        return connection.execute(
            "SELECT id, profile_id, player, started_at, ended_at FROM player_sessions "
            "WHERE profile_id=? ORDER BY started_at, id",
            (profile_id,),
        ).fetchall()
    return connection.execute(
        "SELECT id, profile_id, player, started_at, ended_at FROM player_sessions "
        "WHERE profile_id=? AND (ended_at IS NULL OR ended_at > ?) "
        "ORDER BY started_at, id",
        (profile_id, _iso(cutoff)),
    ).fetchall()


def _interval(
    row: tuple[Any, ...], cutoff: datetime | None, current: datetime
) -> tuple[datetime, datetime] | None:
    started = _parse(row[3])
    ended = min(_parse(row[4]) if row[4] is not None else current, current)
    if cutoff is not None:
        started = max(started, cutoff)
    if ended <= started:
        return None
    return started, ended


def _downsample(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(samples) <= 500:
        return samples
    chunk_size = math.ceil(len(samples) / 500)
    output: list[dict[str, Any]] = []
    for start in range(0, len(samples), chunk_size):
        chunk = samples[start : start + chunk_size]
        output.append(
            {
                "ts": chunk[0]["ts"],
                "tps": sum(item["tps"] for item in chunk) / len(chunk),
                "mspt": sum(item["mspt"] for item in chunk) / len(chunk),
            }
        )
    return output


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["stats_heatmap", "stats_profile_capabilities", "stats_summary", "stats_tps"]
