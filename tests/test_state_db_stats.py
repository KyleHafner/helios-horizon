import sqlite3

import pytest

from game_control import state_db
from game_control.session_store import SessionStore


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    yield connection
    connection.close()


def test_player_sessions_table_accepts_open_session(conn):
    conn.execute(
        "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
        " VALUES ('s1', 'minecraft', 'Swag', '2026-07-14T01:00:00Z', NULL, 'crafty')"
    )
    row = conn.execute("SELECT ended_at FROM player_sessions WHERE id='s1'").fetchone()
    assert row[0] is None


def test_fresh_v4_jobs_schema_has_integer_completion_sequence(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    columns = {row[1]: row[2].upper() for row in conn.execute("PRAGMA table_info(jobs)")}
    assert columns["completion_seq"] == "INTEGER"


def test_player_sessions_rejects_bad_timestamp(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO player_sessions (id, profile_id, player, started_at, ended_at, source)"
            " VALUES ('s2', 'minecraft', 'Swag', 'not-a-time', NULL, 'crafty')"
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


def test_prune_rpc_idempotency_removes_only_expired_completed_rows(conn):
    rows = (
        ("old-completed", "{}", "{}", "completed", "2026-07-10T00:00:00Z"),
        ("old-pending", "{}", "", "pending", "2026-07-10T00:00:00Z"),
        ("recent-completed", "{}", "{}", "completed", "2026-07-13T12:00:01Z"),
        ("offset-completed", "{}", "{}", "completed", "2026-07-11T03:00:00+03:00"),
    )
    conn.executemany(
        "INSERT INTO rpc_idempotency"
        " (request_id, canonical_request, response, status, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )

    removed = state_db.prune_completed_rpc_idempotency(
        conn, now="2026-07-14T00:00:00Z", max_age_hours=48
    )

    assert removed == 2
    assert conn.execute(
        "SELECT request_id FROM rpc_idempotency ORDER BY request_id"
    ).fetchall() == [("old-pending",), ("recent-completed",)]


def test_session_store_hourly_maintenance_prunes_replays_but_preserves_pending(conn):
    conn.executemany(
        "INSERT INTO rpc_idempotency"
        " (request_id, canonical_request, response, status, created_at)"
        " VALUES (?, '{}', ?, ?, ?)",
        [
            ("expired", "{}", "completed", "2026-07-10T00:00:00Z"),
            ("pending", "", "pending", "2026-07-10T00:00:00Z"),
        ],
    )
    store = SessionStore(conn)
    store.record("minecraft", set(), 0, now="2026-07-14T00:00:00Z")
    assert conn.execute("SELECT request_id FROM rpc_idempotency ORDER BY request_id").fetchall() == [("pending",)]

    conn.execute(
        "INSERT INTO rpc_idempotency"
        " (request_id, canonical_request, response, status, created_at)"
        " VALUES ('within-hour', '{}', '{}', 'completed', '2026-07-10T00:00:00Z')"
    )
    store.record("minecraft", set(), 0, now="2026-07-14T00:30:00Z")
    assert conn.execute("SELECT request_id FROM rpc_idempotency WHERE request_id='within-hour'").fetchone() == ("within-hour",)
