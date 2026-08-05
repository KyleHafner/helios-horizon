import sqlite3
from datetime import datetime, timedelta, timezone

from game_control import state_db
from game_control.stats_queries import stats_heatmap, stats_profile_capabilities, stats_summary, stats_tps


NOW = "2026-07-14T02:00:00Z"


def make_conn():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    return connection


def seed(conn):
    rows = [
        ("a", "minecraft", "PlayerOne", "2026-07-13T23:00:00Z", "2026-07-14T01:00:00Z", "crafty"),
        ("b", "minecraft", "Guest", "2026-07-13T23:30:00Z", None, "crafty"),
    ]
    conn.executemany(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )


def test_summary_orders_by_hours_and_counts_open_sessions_to_now():
    conn = make_conn()
    seed(conn)

    result = stats_summary(conn, "minecraft", None, now=NOW)

    assert result["total_hours"] == 4.5
    assert result["unique_players"] == 2
    assert [row["player"] for row in result["leaderboard"]] == ["Guest", "PlayerOne"]
    assert result["leaderboard"][0]["hours"] == 2.5
    assert result["leaderboard"][0]["sessions"] == 1
    assert result["leaderboard"][0]["last_seen"] == NOW


def test_heatmap_splits_session_across_midnight():
    conn = make_conn()
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('a', 'minecraft', 'PlayerOne', '2026-07-13T23:00:00Z', '2026-07-14T01:00:00Z', 'crafty')"
    )

    result = stats_heatmap(conn, "minecraft", 90, now=NOW)

    assert result["days"] == 90
    assert result["buckets"][0][23] == 1.0
    assert result["buckets"][1][0] == 1.0


def test_tps_downsampling_keeps_at_most_500_points():
    conn = make_conn()
    start = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    rows = []
    for index in range(1001):
        timestamp = (start + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
        rows.extend([
            ("minecraft", "tps", timestamp, 20.0 - index / 10000),
            ("minecraft", "mspt", timestamp, 50.0 + index / 100),
        ])
    conn.executemany(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        rows,
    )

    result = stats_tps(conn, "minecraft", "1h", now="2026-07-14T01:00:00Z")

    assert len(result["samples"]) <= 500
    assert set(result["samples"][0]) == {"ts", "tps", "mspt"}


def test_profile_capabilities_are_explicit_and_tick_telemetry_is_minecraft_only():
    assert stats_profile_capabilities("minecraft") == {
        "player_tracking": "names",
        "occupancy": True,
        "tick_telemetry": True,
    }
    assert stats_profile_capabilities("terraria-tmod")["tick_telemetry"] is False
    assert stats_profile_capabilities("pz-rising") == {
        "player_tracking": "count",
        "occupancy": True,
        "tick_telemetry": False,
    }


def test_pz_summary_reports_count_only_occupancy_without_fake_leaderboard():
    conn = make_conn()
    conn.execute(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        ("pz-rising", "players", NOW, 7),
    )

    result = stats_summary(conn, "pz-rising", 90, now=NOW)

    assert result["player_tracking"] == "count"
    assert result["leaderboard"] == []
    assert result["occupancy"]["latest"] == 7
    assert result["occupancy"]["samples"][0] == {"ts": NOW, "count": 7}
