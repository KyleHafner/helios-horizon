import sqlite3
import json
from datetime import datetime, timedelta, timezone

import pytest

from game_control import state_db
from game_control.stats_queries import stats_heatmap, stats_profile_capabilities, stats_summary, stats_tps
from game_control.telemetry_db import TelemetryDatabase


NOW = "2026-07-14T02:00:00Z"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    yield connection
    connection.close()


def seed(conn):
    rows = [
        ("a", "minecraft", "Swag", "2026-07-13T23:00:00Z", "2026-07-14T01:00:00Z", "crafty"),
        ("b", "minecraft", "Guest", "2026-07-13T23:30:00Z", None, "crafty"),
    ]
    conn.executemany(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )


def test_summary_orders_by_hours_and_counts_open_sessions_to_now(conn):
    seed(conn)

    result = stats_summary(conn, "minecraft", None, now=NOW)

    assert result["total_hours"] == 4.5
    assert result["unique_players"] == 2
    assert [row["player"] for row in result["leaderboard"]] == ["Guest", "Swag"]
    assert result["leaderboard"][0]["hours"] == 2.5
    assert result["leaderboard"][0]["sessions"] == 1
    assert result["leaderboard"][0]["last_seen"] == NOW


def test_summary_uses_sql_grouping_with_offset_timestamps_and_ignores_old_history(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('offset', 'minecraft', 'Offset',"
        " '2026-07-13T19:00:00-04:00', '2026-07-13T21:00:00-04:00', 'crafty')"
    )
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('old', 'minecraft', 'Old', '2020-01-01T00:00:00Z',"
        " '2020-01-02T00:00:00Z', 'crafty')"
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    result = stats_summary(conn, "minecraft", 2, now=NOW)

    assert result["unique_players"] == 1
    assert result["leaderboard"] == [
        {"player": "Offset", "hours": 2.0, "sessions": 1, "last_seen": "2026-07-14T01:00:00Z"}
    ]
    assert any("GROUP BY player" in statement for statement in statements)


def test_pz_summary_never_exposes_session_identity(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('private', 'pz-rising', 'SecretName', ?, ?, 'crafty')",
        ("2026-07-14T00:00:00Z", NOW),
    )

    result = stats_summary(conn, "pz-rising", None, now=NOW)

    assert result["unique_players"] == 0
    assert result["total_hours"] == 0.0
    assert result["leaderboard"] == []


def test_heatmap_splits_session_across_midnight(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('a', 'minecraft', 'Swag', '2026-07-13T23:00:00Z', '2026-07-14T01:00:00Z', 'crafty')"
    )

    result = stats_heatmap(conn, "minecraft", 90, now=NOW)

    assert result["days"] == 90
    assert result["buckets"][0][23] == 1.0
    assert result["buckets"][1][0] == 1.0


def test_tps_downsampling_keeps_at_most_500_points(conn):
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


def test_tps_reports_unknown_when_latest_pair_is_stale(conn):
    conn.executemany(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        [
            ("minecraft-sunlit-cobblemon", "tps", "2026-07-14T01:56:00Z", 20.0),
            ("minecraft-sunlit-cobblemon", "mspt", "2026-07-14T01:56:00Z", 12.0),
        ],
    )
    result = stats_tps(conn, "minecraft-sunlit-cobblemon", "1h", now=NOW)
    assert result["stale"] is True
    assert result["state"] == "unknown"
    assert result["latest_ts"] == "2026-07-14T01:56:00Z"


def test_tps_returns_latest_observation_outside_requested_window(conn):
    conn.executemany(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        [
            ("minecraft", "tps", "2026-07-12T01:00:00Z", 19.5),
            ("minecraft", "mspt", "2026-07-12T01:00:00Z", 17.25),
        ],
    )

    result = stats_tps(conn, "minecraft", "24h", now=NOW)

    assert result["samples"] == []
    assert result["latest_ts"] == "2026-07-12T01:00:00Z"
    assert result["latest_observation"] == {
        "ts": "2026-07-12T01:00:00Z",
        "tps": 19.5,
        "mspt": 17.25,
        "stale": True,
        "staleness_seconds": 176400.0,
    }
    assert result["state"] == "unknown"


def test_tps_cutoff_and_order_are_offset_safe(conn):
    conn.executemany(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        [
            ("minecraft", "tps", "2026-07-14T00:30:00-04:00", 19.0),
            ("minecraft", "mspt", "2026-07-14T00:30:00-04:00", 25.0),
            ("minecraft", "tps", "2026-07-14T04:59:00Z", 18.0),
            ("minecraft", "mspt", "2026-07-14T04:59:00Z", 30.0),
        ],
    )
    result = stats_tps(conn, "minecraft", "1h", now="2026-07-14T05:00:00Z")
    assert [sample["tps"] for sample in result["samples"]] == [19.0, 18.0]


def test_heatmap_cutoff_and_session_order_are_offset_safe(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('offset-boundary', 'minecraft', 'Offset', ?, ?, 'crafty')",
        ("2026-07-13T23:30:00-04:00", "2026-07-14T01:00:00-04:00"),
    )
    result = stats_heatmap(conn, "minecraft", 1, now="2026-07-14T05:00:00Z")
    assert sum(sum(day) for day in result["buckets"]) == 1.5


def test_profile_capabilities_are_explicit_and_tick_telemetry_is_minecraft_only():
    assert stats_profile_capabilities("minecraft") == {
        "player_tracking": "names",
        "occupancy": True,
        "tick_telemetry": True,
    }
    assert stats_profile_capabilities("minecraft-sunlit-cobblemon") == {
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


def test_pz_summary_reports_count_only_occupancy_without_fake_leaderboard(conn):
    conn.execute(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        ("pz-rising", "players", NOW, 7),
    )

    result = stats_summary(conn, "pz-rising", 90, now=NOW)

    assert result["player_tracking"] == "count"
    assert result["leaderboard"] == []
    assert result["occupancy"]["latest"] == 7
    assert result["occupancy"]["samples"][0] == {"ts": NOW, "count": 7}


def test_occupancy_limits_latest_samples_then_restores_chronological_order(conn):
    rows = [
        ("minecraft", "players", f"2026-07-13T{i // 60:02d}:{i % 60:02d}:00Z", i)
        for i in range(600)
    ]
    # This equivalent-offset sample is chronologically newest and must be in
    # the bounded result despite lexical ordering differences.
    rows.append(("minecraft", "players", "2026-07-14T06:00:00-04:00", 999))
    conn.executemany(
        "INSERT INTO metric_samples (profile_id, metric, ts, value) VALUES (?,?,?,?)",
        rows,
    )

    result = stats_summary(conn, "minecraft", None, now=NOW)
    samples = result["occupancy"]["samples"]

    assert len(samples) == 500
    assert samples == sorted(samples, key=lambda item: datetime.fromisoformat(item["ts"].replace("Z", "+00:00")))
    assert samples[-1] == {"ts": "2026-07-14T06:00:00-04:00", "count": 999}


def test_v2_raw_resolution_exposes_state_and_time_basis_without_identity(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    try:
        for seconds, state, tps, mspt in (
            (20, "available", 20.0, 10.0),
            (10, "inactive", None, None),
            (5, "unavailable", None, None),
        ):
            stamp = int((now - timedelta(seconds=seconds)).timestamp() * 1000)
            telemetry.record_sample("minecraft", "tps", tps, ts_ms=stamp, state=state, labels={"source": "rcon"})
            telemetry.record_sample("minecraft", "mspt", mspt, ts_ms=stamp, state=state, labels={"source": "rcon"})
        result = stats_tps(
            conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
            resolution="raw", limit=20,
        )
        assert [item["state"] for item in result["samples"]] == ["available", "inactive", "unavailable"]
        assert result["samples"][1]["tps"] is None
        assert result["time_basis"] == {
            "wall_clock_seconds": 3600,
            "active_runtime_seconds": 10.0,
            "inactive_seconds": 5.0,
            "unavailable_seconds": 0.0,
            "observed_seconds": 15.0,
            "unknown_wall_clock_seconds": 3585.0,
        }
        assert "player" not in repr(result).lower()
    finally:
        telemetry.close()


def test_v2_returns_latest_observation_outside_requested_window(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    current = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    stamp = int((current - timedelta(days=2, hours=1)).timestamp() * 1000)
    try:
        telemetry.record_sample("minecraft", "tps", 18.75, ts_ms=stamp, state="available")
        telemetry.record_sample("minecraft", "mspt", 21.5, ts_ms=stamp, state="available")

        result = stats_tps(
            conn, "minecraft", "24h", now=NOW, telemetry=telemetry.connection,
            resolution="raw", limit=20,
        )

        assert result["samples"] == []
        assert result["latest_observation"] == {
            "ts": "2026-07-12T01:00:00Z",
            "tps": 18.75,
            "mspt": 21.5,
            "stale": True,
            "staleness_seconds": 176400.0,
        }
        assert result["latest_ts"] == "2026-07-12T01:00:00Z"
        assert result["state"] == "unknown"
    finally:
        telemetry.close()


def test_v2_rollup_auto_resolution_preserves_extrema_beyond_raw_retention(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    bucket = int((now - timedelta(days=6)).timestamp() * 1000) // 3_600_000 * 3_600_000
    try:
        telemetry.record_rollup("minecraft", "tps", bucket_start_ms=bucket,
                                minimum=17, maximum=20, total=74, count=4)
        telemetry.record_rollup("minecraft", "mspt", bucket_start_ms=bucket,
                                minimum=10, maximum=40, total=80, count=4)
        result = stats_tps(conn, "minecraft", "7d", now=NOW, telemetry=telemetry.connection,
                           resolution="auto", limit=10)
        assert result["resolution"] == "1h"
        assert result["samples"][0]["tps"] == 18.5
        assert result["samples"][0]["tps_min"] == 17.0
        assert result["samples"][0]["tps_max"] == 20.0
        assert result["samples"][0]["state"] == "available"
    finally:
        telemetry.close()


def test_v2_explicit_subhour_resolution_reports_effective_hourly_rollup(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    bucket = int((now - timedelta(days=6)).timestamp() * 1000) // 3_600_000 * 3_600_000
    try:
        for metric, value in (("tps", 20), ("mspt", 12)):
            telemetry.record_rollup("minecraft", metric, bucket_start_ms=bucket,
                                    minimum=value, maximum=value, total=value, count=1)
        result = stats_tps(conn, "minecraft", "7d", now=NOW, telemetry=telemetry.connection,
                           resolution="5m", limit=10)
        assert result["requested_resolution"] == "5m"
        assert result["resolution"] == "1h"
    finally:
        telemetry.close()


def test_v2_downsampling_preserves_extrema_and_state_boundaries(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    try:
        for index in range(20):
            stamp = int((now - timedelta(seconds=100 - index * 5)).timestamp() * 1000)
            state = "inactive" if 8 <= index <= 10 else "available"
            tps = None if state != "available" else (1.0 if index == 3 else 25.0 if index == 16 else 20.0)
            mspt = None if state != "available" else 50.0
            telemetry.record_sample("minecraft", "tps", tps, ts_ms=stamp, state=state)
            telemetry.record_sample("minecraft", "mspt", mspt, ts_ms=stamp, state=state)
        result = stats_tps(conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
                           resolution="raw", limit=8)
        assert len(result["samples"]) <= 8
        assert {item["state"] for item in result["samples"]} == {"available", "inactive"}
        assert 1.0 in {item["tps"] for item in result["samples"]}
        assert 25.0 in {item["tps"] for item in result["samples"]}
    finally:
        telemetry.close()


def test_v2_timeline_context_is_bounded_identity_free_and_fixed_job_kinds(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    conn.executemany(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at) VALUES(?,?,?,?,?,?)",
        [
            ("1", "minecraft", "scheduled_backup", "completed", "2026-07-14T01:30:00Z", "2026-07-14T01:31:00Z"),
            ("2", "minecraft", "command", "completed", "2026-07-14T01:40:00Z", "2026-07-14T01:40:01Z"),
        ],
    )
    try:
        for index in range(8):
            stamp = int((now - timedelta(minutes=8 - index)).timestamp() * 1000)
            for metric, value in (("tps", 20), ("mspt", 12), ("cpu_percent", index * 10), ("rss_bytes", 1000 + index), ("gc_pause", index)):
                telemetry.record_sample("minecraft", metric, value, ts_ms=stamp, state="available")
        result = stats_tps(conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
                           resolution="1m", limit=5)
        assert set(result["context"]["series"]) == {"cpu_percent", "rss_bytes", "gc_pause"}
        assert all(len(points) <= 5 for points in result["context"]["series"].values())
        assert [item["kind"] for item in result["context"]["jobs"]] == ["backup"]
        assert "command" not in repr(result["context"])
    finally:
        telemetry.close()


def test_flight_recorder_returns_real_gc_job_and_three_bounded_comparison_modes(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    conn.executemany(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at) VALUES(?,?,?,?,?,?)",
        [
            ("restart-job", "minecraft", "restart", "succeeded", "2026-07-14T01:00:00Z", "2026-07-14T01:02:00Z"),
            ("update-job", "minecraft", "update", "succeeded", "2026-07-14T01:20:00Z", "2026-07-14T01:25:00Z"),
            ("benchmark-job", "minecraft", "benchmark", "succeeded", "2026-07-14T01:30:00Z", "2026-07-14T01:40:00Z"),
        ],
    )
    conn.execute(
        "INSERT INTO backups(id,profile_id,created_at,size_bytes,verified,protected) VALUES(?,?,?,?,?,?)",
        ("private-backup-id", "minecraft", "2026-07-14T01:10:00Z", 1024, 1, 0),
    )
    conn.execute(
        "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("bench-1", "minecraft", "current", "candidate", "succeeded", "2026-07-14T00:00:00Z",
         "2026-07-14T01:40:00Z", "better", json.dumps({"metrics": [{"name": "tick.p95Nanos",
         "baselineMedian": 20_000_000, "candidateMedian": 15_000_000}]})),
    )
    try:
        for stamp, tps, mspt in (
            (now - timedelta(days=1, hours=2), 18, 24),
            (now - timedelta(days=1, hours=1), 19, 20),
            (now - timedelta(minutes=90), 17, 28),
            (now - timedelta(minutes=30), 20, 12),
        ):
            ts_ms = int(stamp.timestamp() * 1000)
            telemetry.record_sample("minecraft", "tps", tps, ts_ms=ts_ms, state="available")
            telemetry.record_sample("minecraft", "mspt", mspt, ts_ms=ts_ms, state="available")
        telemetry.record_sample("minecraft", "gc_pause", 87, ts_ms=int((now - timedelta(minutes=25)).timestamp() * 1000), state="available")

        result = stats_tps(conn, "minecraft", "6h", now=NOW, telemetry=telemetry.connection,
                           resolution="raw", limit=20)

        assert result["context"]["series"]["gc_pause"][0]["value"] == 87
        assert {item["kind"] for item in result["context"]["jobs"]} >= {"restart", "backup", "update", "benchmark"}
        assert "private-backup-id" not in repr(result["context"])
        assert result["comparisons"]["yesterday"]["samples"][0]["tps"] == 18
        assert result["comparisons"]["restart"]["samples"][0]["tps"] == 17
        assert result["comparisons"]["preset"]["metrics"]["mspt_p95"] == {
            "baseline": 20.0, "candidate": 15.0, "unit": "ms"
        }
        assert set(result["comparisons"]["preset"]) == {
            "label", "baseline_preset", "candidate_preset", "verdict", "metrics"
        }
    finally:
        telemetry.close()


def test_v2_exposes_previous_active_run_only_when_state_boundary_exists(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    try:
        for index, state in enumerate(("available", "available", "inactive", "available")):
            stamp = int((now - timedelta(seconds=20 - index * 5)).timestamp() * 1000)
            for metric in ("tps", "mspt"):
                telemetry.record_sample("minecraft", metric, 20 if metric == "tps" and state == "available" else 10 if state == "available" else None,
                                        ts_ms=stamp, state=state)
        result = stats_tps(conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
                           resolution="raw", limit=20)
        assert result["comparisons"]["previous"]["label"] == "Previous active run"
        assert len(result["comparisons"]["previous"]["samples"]) == 2
    finally:
        telemetry.close()


def test_v2_hourly_rollups_preserve_long_inactive_state_and_true_duration(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", retention_ms=60_000)
    now = "1970-01-01T02:00:00Z"
    try:
        for stamp, state in ((0, "available"), (1_800_000, "inactive"),
                             (3_600_000, "unavailable"), (5_400_000, "inactive")):
            for metric, value in (("tps", 20), ("mspt", 10)):
                telemetry.record_sample("minecraft", metric, value if state == "available" else None,
                                        ts_ms=stamp, state=state)
        telemetry.compact_hourly(now_ms=7_200_000)
        result = stats_tps(conn, "minecraft", "7d", now=now, telemetry=telemetry.connection,
                           resolution="1h", limit=20)
        basis = result["time_basis"]
        assert basis["active_runtime_seconds"] >= 1_700
        assert basis["inactive_seconds"] >= 3_500
        assert basis["unavailable_seconds"] >= 1_700
        assert any(sample.get("inactive_fraction", 0) > 0 for sample in result["samples"])
        assert basis["observed_seconds"] <= basis["wall_clock_seconds"]
    finally:
        telemetry.close()


def test_v2_subhour_aggregation_keeps_mixed_state_fractions(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    try:
        for seconds, state in ((50, "available"), (40, "inactive"), (30, "available")):
            stamp = int((now - timedelta(seconds=seconds)).timestamp() * 1000)
            for metric, value in (("tps", 20), ("mspt", 10)):
                telemetry.record_sample("minecraft", metric, value if state == "available" else None,
                                        ts_ms=stamp, state=state)
        result = stats_tps(conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
                           resolution="1m", limit=20)
        mixed = next(sample for sample in result["samples"] if sample.get("inactive_fraction", 0) > 0)
        assert mixed["available_fraction"] > 0
        assert mixed["state"] == "available"
    finally:
        telemetry.close()


def test_v2_many_transitions_keep_latest_and_extrema_when_boundaries_exceed_limit(conn, tmp_path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    try:
        stamps = []
        for index in range(40):
            stamp = int((now - timedelta(seconds=200 - index * 5)).timestamp() * 1000); stamps.append(stamp)
            state = "available" if index % 2 == 0 else "inactive"
            for metric in ("tps", "mspt"):
                value = (1 if index == 4 else 30 if index == 38 else 20) if metric == "tps" else 10
                telemetry.record_sample("minecraft", metric, value if state == "available" else None,
                                        ts_ms=stamp, state=state)
        result = stats_tps(conn, "minecraft", "1h", now=NOW, telemetry=telemetry.connection,
                           resolution="raw", limit=8)
        returned = result["samples"]
        assert len(returned) == 8 and result["state_boundaries_truncated"] is True
        assert returned[0]["ts"] == datetime.fromtimestamp(stamps[0] / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        assert returned[-1]["ts"] == datetime.fromtimestamp(stamps[-1] / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
        assert {sample["tps"] for sample in returned if sample["tps"] is not None} >= {1.0, 30.0}
    finally:
        telemetry.close()


def test_heatmap_cache_is_idempotent_and_bounded_query_equivalent(conn):
    seed(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    first = stats_heatmap(conn, "minecraft", 90, now=NOW)
    second = stats_heatmap(conn, "minecraft", 90, now=NOW)
    assert first == second
    session_reads = [statement for statement in statements if "SELECT started_at, ended_at" in statement]
    assert len(session_reads) == 1
    assert first["cache_strategy"] == "hour_bucket_bounded_on_demand"
    assert first["truncated"] is False
    assert first["as_of"].endswith(":00:00Z")


def test_heatmap_cache_reuses_hour_bucket_for_normal_polling(conn):
    seed(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    first = stats_heatmap(conn, "minecraft", 90, now="2026-07-14T06:01:00Z")
    second = stats_heatmap(conn, "minecraft", 90, now="2026-07-14T06:59:59Z")
    assert first == second
    session_reads = [statement for statement in statements if "SELECT started_at, ended_at" in statement]
    assert len(session_reads) == 1


def test_heatmap_first_materialization_is_bounded_and_reports_truncation(conn):
    rows = [
        (f"session-{index}", "minecraft", f"player-{index}",
         "2026-07-14T05:00:00Z", "2026-07-14T05:01:00Z", "crafty")
        for index in range(2_050)
    ]
    conn.executemany(
        "INSERT INTO player_sessions(id,profile_id,player,started_at,ended_at,source) VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)

    result = stats_heatmap(conn, "minecraft", 90, now=NOW)

    assert result["truncated"] is True
    reads = [statement for statement in statements if "SELECT started_at, ended_at" in statement]
    assert len(reads) == 1
    assert "LIMIT 2049" in reads[0]
