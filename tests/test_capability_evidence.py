from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from game_control import state_db
from game_control.capability_evidence import RootWakeSafetyEvidence
from game_control.models import ProfileId
from game_control.slot import ReservationStore


def _root_db():
    connection = sqlite3.connect(":memory:")
    state_db._configure(connection)
    state_db._migrate_state(connection)
    return connection


def test_root_evidence_uses_jobs_and_reservation_not_web_db(tmp_path: Path):
    root = _root_db()
    reservation = tmp_path / "reservation.json"
    store = ReservationStore(tmp_path / "operation.lock", reservation)
    evidence = RootWakeSafetyEvidence(SimpleNamespace(connection=root), store)

    # This is the installed web-owner shape: private directory and database,
    # with a pending capability request. Root evidence must not inspect it.
    web_dir = tmp_path / "game-control-web"
    web_dir.mkdir(mode=0o700)
    web_db = web_dir / "web.db"
    web_db.write_bytes(b"web-owned pending wake")
    web_db.chmod(0o600)
    assert evidence().clear is True

    root.execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at) VALUES(?,?,?,?,?)",
        ("wake-job", ProfileId.MINECRAFT.value, "start", "accepted", "2026-08-29T00:00:00Z"),
    )
    root.commit()
    assert evidence().clear is False


def test_existing_malformed_reservation_fails_closed(tmp_path: Path):
    root = _root_db()
    operation = tmp_path / "operation.lock"
    operation.touch()
    reservation = tmp_path / "reservation.json"
    reservation.write_text("{not-json")
    store = ReservationStore(operation, reservation)

    result = RootWakeSafetyEvidence(SimpleNamespace(connection=root), store)()
    assert result.available is False
    assert result.clear is False


def test_unavailable_root_state_fails_closed(tmp_path: Path):
    reservation = tmp_path / "reservation.json"
    store = SimpleNamespace(reservation_path=reservation, read=lambda: None)
    result = RootWakeSafetyEvidence(object(), store)()
    assert result.available is False
    assert result.clear is False
