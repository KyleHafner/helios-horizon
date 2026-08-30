import sqlite3

import pytest

from game_control import state_db
from game_control.session_store import SessionStore


T0 = "2026-07-14T01:00:00Z"
T1 = "2026-07-14T01:05:00Z"


@pytest.fixture
def store_and_conn():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    yield SessionStore(connection), connection
    connection.close()


def test_record_persists_open_then_close(store_and_conn):
    store, conn = store_and_conn
    store.record("minecraft", {"Swag"}, 1, now=T0)
    assert conn.execute(
        "SELECT COUNT(*) FROM player_sessions WHERE ended_at IS NULL"
    ).fetchone()[0] == 1
    store.record("minecraft", set(), 0, now=T1)
    row = conn.execute("SELECT ended_at FROM player_sessions").fetchone()
    assert row[0] == T1


def test_count_sample_written_on_change_only(store_and_conn):
    store, conn = store_and_conn
    store.record("minecraft", {"Swag"}, 1, now=T0)
    store.record("minecraft", {"Swag"}, 1, now=T1)
    rows = conn.execute(
        "SELECT COUNT(*) FROM metric_samples WHERE metric='players'"
    ).fetchone()[0]
    assert rows == 1


def test_recover_closes_dangling(store_and_conn):
    store, conn = store_and_conn
    store.record("minecraft", {"Swag"}, 1, now=T0)
    fresh = SessionStore(conn)
    assert fresh.recover(now=T1) == 1
    row = conn.execute("SELECT ended_at, source FROM player_sessions").fetchone()
    assert row == (T1, "recovered")
