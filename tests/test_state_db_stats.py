import sqlite3

import pytest

from game_control import state_db


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    return connection


def test_player_sessions_table_accepts_open_session(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('s1', 'minecraft', 'PlayerOne', '2026-07-14T01:00:00Z', NULL, 'crafty')"
    )
    row = conn.execute("SELECT ended_at FROM player_sessions WHERE id='s1'").fetchone()
    assert row[0] is None


def test_player_sessions_rejects_bad_timestamp(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
            " VALUES ('s2', 'minecraft', 'PlayerOne', 'not-a-time', NULL, 'crafty')"
        )


def test_prune_metric_samples_removes_only_old_rows(conn):
    conn.execute(
        "INSERT INTO metric_samples (profile_id, metric, ts, value)"
        " VALUES ('minecraft', 'tps', '2026-05-01T00:00:00Z', 20.0)"
    )
    conn.execute(
        "INSERT INTO metric_samples (profile_id, metric, ts, value)"
        " VALUES ('minecraft', 'tps', '2026-07-13T00:00:00Z', 19.5)"
    )
    removed = state_db.prune_metric_samples(conn, now="2026-07-14T00:00:00Z")
    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 1


def test_metric_samples_accept_perf_aggregates(conn):
    conn.execute(
        "INSERT INTO metric_samples (profile_id, metric, ts, value)"
        " VALUES ('slotd', 'perf.rpc_p95_ms', '2026-07-15T00:00:00Z', 4.5)"
    )
    assert conn.execute("SELECT value FROM metric_samples WHERE profile_id='slotd'").fetchone()[0] == 4.5
