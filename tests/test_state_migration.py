from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from game_control import state_db


UTILITY_PATH = Path(__file__).parents[1] / "ops/bin/horizon-state-migrate"
loader = importlib.machinery.SourceFileLoader("horizon_state_migrate", str(UTILITY_PATH))
spec = importlib.util.spec_from_loader("horizon_state_migrate", loader)
assert spec and spec.loader
migration = importlib.util.module_from_spec(spec)
loader.exec_module(migration)


TS = "2026-08-05T12:00:00Z"


def make_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    state_db._configure(connection)
    state_db._migrate_state(connection)
    connection.commit()
    connection.close()
    path.chmod(0o600)


def make_legacy_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    state_db._configure(connection)
    for table in (
        "events",
        "audit",
        "jobs",
        "confirmations",
        "backups",
        "notification_rules",
        "notification_deliveries",
        "updates",
        "rpc_idempotency",
        "player_sessions",
        "metric_samples",
    ):
        connection.execute(migration.LEGACY_TABLE_SQL[table])
    for index_name in sorted(migration.LEGACY_SOURCE_INDEXES):
        connection.execute(migration.EXPECTED_INDEX_SQL[index_name])
    for trigger_name in sorted(migration.EXPECTED_TRIGGERS):
        connection.execute(migration.EXPECTED_TRIGGER_SQL[trigger_name])
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    path.chmod(0o600)


@pytest.fixture(autouse=True)
def no_test_process_is_a_writer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(migration, "_writer_processes", lambda: [])


def paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    report = tmp_path / "report.json"
    retired = tmp_path / "retired.json"
    return source, target, report, retired


def run(source: Path, target: Path, report: Path, retired: Path, **kwargs):
    return migration.migrate(
        source,
        target,
        report,
        retired,
        **kwargs,
    )


def test_cli_argument_names_map_to_migration_paths(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)

    assert migration.main([
        "--source", str(source),
        "--target", str(target),
        "--report", str(report),
        "--retired-export", str(retired),
    ]) == 0
    assert target.is_file()
    assert report.is_file()
    assert retired.is_file()


def test_migrates_every_actual_state_table_and_exact_retained_filter(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    connection = sqlite3.connect(source)
    state_db._configure(connection)
    connection.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?)",
        [("e-good", TS, "minecraft-sunlit-cobblemon", "start", "ok"), ("e-old", TS, "minecraft", "start", "old")],
    )
    connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", ("a-good", TS, "operator", "start", "minecraft-sunlit-cobblemon", "ok", None, "done"))
    connection.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)",
        [("j-good", "terraria-vanilla", "stop", "succeeded", TS, TS, 1, "done"), ("j-running", "terraria-vanilla", "start", "running", TS, None, None, "live")],
    )
    connection.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?)", ("c1", "operator", "stop", "terraria-tmod", "opaque-secret", TS, None, 1))
    connection.execute("INSERT INTO backups VALUES (?,?,?,?,?,?)", ("b1", "terraria-tmod", TS, 10, 1, 1))
    connection.execute(
        "INSERT INTO backup_protections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "b1",
            "terraria-tmod",
            "horizon-b2",
            "application",
            "remote/b1",
            "a" * 64,
            1,
            "succeeded",
            1,
            "verified",
            "succeeded",
            TS,
            None,
        ),
    )
    connection.execute("INSERT INTO notification_rules VALUES (?,?,?)", ("minecraft-sunlit-cobblemon", "failed", 1))
    connection.execute("INSERT INTO notification_deliveries VALUES (?,?,?,?,?,?,?)", ("d1", "terraria-vanilla", "failed", 2, "mail", TS, None))
    connection.execute("INSERT INTO updates VALUES (?,?,?,?,?,?,?)", ("u1", "terraria-tmod", TS, "image", "1", "2", "succeeded"))
    connection.execute("INSERT INTO rpc_idempotency VALUES (?,?,?,?,?)", ("r1", "{}", "secret-response", "completed", TS))
    connection.execute("INSERT INTO player_sessions VALUES (?,?,?,?,?,?)", ("s1", "minecraft-sunlit-cobblemon", "Player", TS, TS, "log"))
    connection.execute("INSERT INTO player_sessions VALUES (?,?,?,?,?,?)", ("s2", "minecraft-sunlit-cobblemon", "Current", TS, None, "log"))
    connection.execute("INSERT INTO metric_samples VALUES (?,?,?,?)", ("terraria-tmod", "players", TS, 2.0))
    connection.executemany(
        "INSERT INTO benchmark_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("br-good", "minecraft-sunlit-cobblemon", "current", "balanced-g1", "succeeded", TS, TS, "inconclusive", "{}", "/var/lib/game-control/benchmarks/br-good", None),
            ("br-running", "minecraft-sunlit-cobblemon", "current", "balanced-g1", "running", TS, None, None, None, None, None),
        ],
    )
    connection.commit()
    connection.close()

    result = run(source, target, report, retired)
    assert result["quick_check"] == {"source": "ok", "target": "ok"}
    target_db = sqlite3.connect(target)
    counts = {
        table: target_db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in migration.EXPECTED_TABLE_SQL
    }
    target_db.close()
    assert counts == {
        "events": 1,
        "audit": 1,
        "jobs": 1,
        "confirmations": 0,
        "backups": 1,
        "backup_protections": 1,
        "notification_rules": 1,
        "notification_deliveries": 1,
        "updates": 1,
        "rpc_idempotency": 0,
        "player_sessions": 1,
        "metric_samples": 1,
        "benchmark_runs": 1,
    }
    assert result["excluded_counts_by_table_reason"]["jobs"] == {"unfinished_job": 1}
    assert result["excluded_counts_by_table_reason"]["confirmations"] == {"transient_confirmation": 1}
    assert result["excluded_counts_by_table_reason"]["rpc_idempotency"] == {"transient_idempotency": 1}
    assert result["excluded_counts_by_table_reason"]["player_sessions"] == {"active_session": 1}
    assert result["excluded_counts_by_table_reason"]["benchmark_runs"] == {"unfinished_benchmark": 1}
    assert json.loads(retired.read_text())["row_count"] == 1
    target_check = sqlite3.connect(target)
    assert target_check.execute("PRAGMA foreign_key_check").fetchall() == []
    assert target_check.execute(
        "SELECT remote_key FROM backup_protections"
    ).fetchone() == ("remote/b1",)
    assert target_check.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='backup_protections'"
    ).fetchone()[0] == migration.EXPECTED_TABLE_SQL["backup_protections"]
    assert target_check.execute("PRAGMA foreign_key_list(backup_protections)").fetchall() == [
        (0, 0, "backups", "backup_id", "id", "NO ACTION", "RESTRICT", "NONE")
    ]
    assert target_check.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_backup_protections_scope'"
    ).fetchone()[0] == migration.EXPECTED_INDEX_SQL["idx_backup_protections_scope"]
    for trigger_name, trigger_sql in migration.EXPECTED_TRIGGER_SQL.items():
        assert target_check.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger_name,)
        ).fetchone()[0] == trigger_sql
    target_check.close()


def test_legacy_and_unknown_evidence_never_contains_secret_text(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    connection = sqlite3.connect(source)
    state_db._configure(connection)
    connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", ("legacy", TS, "operator", "start", "minecraft", "failed", None, "TOP-SECRET token=do-not-export"))
    connection.execute("INSERT INTO events VALUES (?,?,?,?,?)", ("unknown", TS, "mystery-profile", "start", "PRIVATE-MATERIAL"))
    connection.commit()
    connection.close()
    result = run(source, target, report, retired)
    evidence = retired.read_bytes()
    assert b"TOP-SECRET" not in evidence
    assert b"PRIVATE-MATERIAL" not in evidence
    assert b"do-not-export" not in evidence
    assert b"TOP-SECRET" not in report.read_bytes()
    assert b"PRIVATE-MATERIAL" not in report.read_bytes()
    assert retired.stat().st_mode & 0o777 == 0o400
    assert result["retired_export_sha256"]


def test_unknown_schema_lock_and_link_safety_fail_closed(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE future_table (id TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(migration.MigrationError, match="table set"):
        run(source, target, report, retired)

    source.unlink()
    make_db(source)
    lock = sqlite3.connect(source, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    with pytest.raises(migration.MigrationError, match="lock"):
        run(source, target, report, retired)
    lock.rollback()
    lock.close()

    source.unlink()
    make_db(source)
    hardlink = tmp_path / "hardlink.db"
    os.link(source, hardlink)
    with pytest.raises(migration.MigrationError, match="hard-linked"):
        run(hardlink, target, report, retired)
    symlink = tmp_path / "symlink.db"
    symlink.symlink_to(source)
    with pytest.raises(migration.MigrationError, match="regular"):
        run(symlink, target, report, retired)


def test_atomic_failure_leaves_no_target_or_temp_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    original_replace = migration.os.replace

    def fail_replace(old, new):
        if Path(new) == target:
            raise OSError("injected atomic failure")
        return original_replace(old, new)

    monkeypatch.setattr(migration.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic failure"):
        run(source, target, report, retired)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.db.tmp-*"))


def test_hashes_quick_check_and_generated_target_guard(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    first = run(source, target, report, retired)
    assert first["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert first["target_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    with pytest.raises(migration.MigrationError, match="non-empty target"):
        run(source, target, tmp_path / "second-report.json", tmp_path / "second-retired.json")
    second = run(source, target, tmp_path / "second-report.json", tmp_path / "second-retired.json", replace_empty_generated_target=True)
    assert second["target_sha256"]


def test_writer_gate_and_wal_shm_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    monkeypatch.setattr(migration, "_writer_processes", lambda: [1234])
    with pytest.raises(migration.MigrationError, match="writer process"):
        run(source, target, report, retired)
    monkeypatch.setattr(migration, "_writer_processes", lambda: [])
    Path(f"{source}-shm").write_bytes(b"unsafe")
    Path(f"{source}-shm").chmod(0o600)
    with pytest.raises(migration.MigrationError, match="without WAL"):
        run(source, target, report, retired)


def test_exact_live_legacy_schema_upgrades_to_canonical_b2_target(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_legacy_db(source)
    connection = sqlite3.connect(source)
    state_db._configure(connection)
    connection.execute(
        "INSERT INTO jobs(id,profile_id,operation,state,created_at,finished_at,detail,completion_seq) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("legacy-job", "terraria-vanilla", "stop", "succeeded", TS, TS, "done", 4),
    )
    connection.execute("INSERT INTO backups VALUES (?,?,?,?,?,?)", ("legacy-backup", "terraria-vanilla", TS, 8, 1, 0))
    connection.commit()
    connection.close()

    result = run(source, target, report, retired)
    assert result["included_counts_by_table"]["backup_protections"] == 0
    target_db = sqlite3.connect(target)
    assert [row[1] for row in target_db.execute("PRAGMA table_info(jobs)")] == [
        column[0] for column in migration.EXPECTED_COLUMNS["jobs"]
    ]
    assert target_db.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 0
    assert target_db.execute("SELECT COUNT(*) FROM backups").fetchone()[0] == 1
    assert target_db.execute("PRAGMA foreign_key_list(backup_protections)").fetchall()
    assert target_db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='backups'"
    ).fetchone()[0] == migration.EXPECTED_TABLE_SQL["backups"]
    target_db.close()

    rerun = run(
        source,
        target,
        tmp_path / "rerun-report.json",
        tmp_path / "rerun-retired.json",
        replace_empty_generated_target=True,
    )
    assert rerun["target_sha256"]


@pytest.mark.parametrize("malicious_object", ["index", "trigger", "foreign_key", "ddl"])
def test_malicious_schema_objects_and_ddl_are_rejected(tmp_path: Path, malicious_object: str):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    connection = sqlite3.connect(source)
    if malicious_object == "index":
        connection.execute("CREATE INDEX malicious_index ON events(id)")
    elif malicious_object == "trigger":
        connection.execute(
            "CREATE TRIGGER malicious_trigger AFTER INSERT ON events BEGIN SELECT 1; END"
        )
    else:
        connection.execute("PRAGMA writable_schema = ON")
        if malicious_object == "foreign_key":
            table = "backup_protections"
            current = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            current = current.replace(
                "UNIQUE (remote_key)",
                "FOREIGN KEY (profile_id) REFERENCES backups(profile_id), UNIQUE (remote_key)",
            )
        else:
            table = "events"
            current = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            current = current.replace("message TEXT NOT NULL", "message TEXT")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (current, table),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    connection.commit()
    connection.close()
    with pytest.raises(migration.MigrationError):
        run(source, target, report, retired)


def test_source_evidence_records_wal_sidecars_and_logical_hash(tmp_path: Path):
    source, target, report, retired = paths(tmp_path)
    make_db(source)
    result = run(source, target, report, retired)
    assert result["source_wal_sha256"]
    assert result["source_wal_bytes"] is not None
    assert result["source_shm_sha256"]
    assert result["source_shm_bytes"] is not None
    assert len(result["source_logical_sha256"]) == 64


def test_retired_row_hash_ignores_secret_and_free_text_changes(tmp_path: Path):
    def migrate_secret(secret: str, suffix: str) -> str:
        source = tmp_path / f"source-{suffix}.db"
        target = tmp_path / f"target-{suffix}.db"
        report = tmp_path / f"report-{suffix}.json"
        retired = tmp_path / f"retired-{suffix}.json"
        make_legacy_db(source)
        connection = sqlite3.connect(source)
        state_db._configure(connection)
        connection.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)",
            ("legacy", TS, "operator", "start", "minecraft", "failed", None, secret),
        )
        connection.commit()
        connection.close()
        run(source, target, report, retired)
        return json.loads(retired.read_text())["rows"][0]["row_sha256"]

    first = migrate_secret("secret-one", "one")
    second = migrate_secret("secret-two", "two")
    assert first == second
