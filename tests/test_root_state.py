from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from game_control import state_db
from game_control.root_state import RootActiveJobsReader, RootGenerationReader


def _root_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    return connection


def test_active_jobs_reader_returns_typed_root_results():
    connection = _root_db()
    reader = RootActiveJobsReader(SimpleNamespace(connection=connection))

    empty = reader.read("minecraft")
    assert empty.available is True
    assert empty.value is None
    assert reader.any_active().value is False

    connection.execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at) VALUES(?,?,?,?,?)",
        ("job-1", "minecraft", "start", "accepted", "2026-08-29T00:00:00Z"),
    )
    connection.commit()
    active = reader.read("minecraft")
    assert active.available is True
    assert active.value == "start"
    assert reader.any_active().value is True
    assert reader("minecraft") == "start"


def test_root_readers_fail_closed_when_root_connection_is_unavailable():
    jobs = RootActiveJobsReader(object())
    generation = RootGenerationReader(object())

    assert jobs.any_active().available is False
    assert jobs.read("minecraft").available is False
    assert generation.read().available is False
    assert generation() == 0


def test_generation_reader_is_read_only_projection():
    connection = _root_db()
    connection.execute("PRAGMA application_id = 17")
    reader = RootGenerationReader(connection)

    result = reader.read()
    assert result.available is True
    assert result.value == 17
    assert reader() == 17
