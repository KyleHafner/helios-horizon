from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from game_control.telemetry_migration import MigrationError, migrate


def make_source(path, rows):
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS metric_samples")
        db.create_function("is_rfc3339_timestamp", 1, lambda value: 1)
        db.execute("CREATE TABLE metric_samples (profile_id TEXT NOT NULL, metric TEXT NOT NULL CHECK (metric IN ('tps', 'mspt', 'players') OR metric LIKE 'perf.%'), ts TEXT NOT NULL CHECK (is_rfc3339_timestamp(ts) = 1), value REAL NOT NULL)")
        db.execute("CREATE INDEX idx_metric_samples ON metric_samples(profile_id, metric, ts)")
        # The migration source is a state-v3 image.  These unrelated indexes
        # and append-only guards are legitimate controller-owned objects and
        # must remain part of the exact allowlist exercised by this fixture.
        for trigger in ("events_append_only_update", "events_append_only_delete", "audit_append_only_update", "audit_append_only_delete"):
            db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for index in ("idx_player_sessions_profile", "idx_backup_protections_scope", "idx_benchmark_runs_profile"):
            db.execute(f"DROP INDEX IF EXISTS {index}")
        for table in ("player_sessions", "backup_protections", "benchmark_runs", "events", "audit"):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute("CREATE TABLE player_sessions(profile_id TEXT, started_at TEXT)")
        db.execute("CREATE INDEX idx_player_sessions_profile ON player_sessions(profile_id, started_at)")
        db.execute("CREATE TABLE backup_protections(profile_id TEXT, destination_id TEXT, backup_class TEXT, remote_verified INTEGER, comparison_state TEXT)")
        db.execute("CREATE INDEX idx_backup_protections_scope ON backup_protections(profile_id, destination_id, backup_class, remote_verified, comparison_state)")
        db.execute("CREATE TABLE benchmark_runs(profile_id TEXT, created_at TEXT)")
        db.execute("CREATE INDEX idx_benchmark_runs_profile ON benchmark_runs(profile_id, created_at DESC)")
        db.execute("CREATE TABLE events(id TEXT)")
        db.execute("CREATE TABLE audit(id TEXT)")
        db.execute("CREATE TRIGGER events_append_only_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
        db.execute("CREATE TRIGGER events_append_only_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END")
        db.execute("CREATE TRIGGER audit_append_only_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END")
        db.execute("CREATE TRIGGER audit_append_only_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END")
        db.execute("PRAGMA user_version=3")
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.executemany("INSERT INTO metric_samples VALUES (?,?,?,?)", rows)
    path.chmod(0o600)


def make_v1(path, rows, *, user_version=1, exact_schema=True):
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE IF EXISTS resource_samples")
        if exact_schema:
            db.execute("CREATE TABLE resource_samples(profile_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, metric TEXT NOT NULL CHECK (metric IN ('cpu_percent', 'rss_bytes', 'disk_read_bps', 'disk_write_bps')), value REAL, state TEXT NOT NULL CHECK (state IN ('available', 'inactive', 'unavailable')), PRIMARY KEY(profile_id, ts_ms, metric))")
            db.execute("CREATE INDEX resource_samples_ts ON resource_samples(ts_ms)")
        else:
            db.execute("CREATE TABLE resource_samples(profile_id TEXT, ts_ms TEXT, metric TEXT, value TEXT, state TEXT)")
        db.execute(f"PRAGMA user_version={user_version}")
        db.executemany("INSERT INTO resource_samples VALUES (?,?,?,?,?)", rows)
    path.chmod(0o600)


def copy_db(source, backup):
    backup.write_bytes(source.read_bytes())
    backup.chmod(0o600)


def test_dry_run_import_and_idempotency_preserve_source(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    rows = [("minecraft-sunlit-cobblemon", "players", "2026-08-23T12:00:00Z", 2.0), ("minecraft-sunlit-cobblemon", "perf.cpu_percent", "2026-08-23T12:00:01Z", 10.0)]
    make_source(source, rows); backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    before = source.read_bytes()
    assert migrate(source, target, backup, dry_run=True, writer_probe=lambda: False)["sourceRows"] == 2
    first = migrate(source, target, backup, writer_probe=lambda: False)
    second = migrate(source, target, backup, writer_probe=lambda: False)
    assert first["importedRows"] == 2 and second["idempotent"] is True
    assert source.read_bytes() == before
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT COUNT(*) FROM telemetry_samples").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM migration_ledger").fetchone()[0] == 1


def test_live_v1_resource_shape_states_and_separate_hashes(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "players", "2026-08-23T12:00:00Z", 2.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    telemetry = tmp_path / "telemetry-v1.db"
    make_v1(telemetry, [
        ("minecraft", 1787486400000, "cpu_percent", 10.0, "available"),
        ("minecraft", 1787486401000, "rss_bytes", None, "inactive"),
        ("minecraft", 1787486402000, "disk_read_bps", None, "unavailable"),
        ("minecraft", 1787486403000, "disk_write_bps", 3.0, "available"),
    ])
    telemetry_backup = tmp_path / "telemetry-v1-backup.db"; copy_db(telemetry, telemetry_backup)
    result = migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)
    assert result["telemetrySourceRows"] == 4 and result["importedRows"] == 5
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT state,expected,value FROM telemetry_samples WHERE state!='available' ORDER BY ts_ms").fetchall() == [("inactive", 0, None), ("unavailable", 1, None)]
        assert len(db.execute("PRAGMA table_info(migration_ledger)").fetchall()) == 12
        assert db.execute("SELECT source_rows,state_rows,telemetry_rows,imported_rows FROM migration_ledger").fetchone() == (5, 1, 4, 5)


def test_v1_live_shape_and_all_perf_metrics_are_imported(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    base = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    state_rows = [("minecraft", metric, (base.replace(second=i)).isoformat().replace("+00:00", "Z"), float(i + 1))
                  for i, metric in enumerate(("perf.cycle_avg_ms", "perf.cycle_max_ms", "perf.cycle_p95_ms", "perf.rpc_avg_ms", "perf.rpc_max_ms", "perf.rpc_p95_ms"))]
    make_source(source, state_rows); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    make_v1(telemetry, [("minecraft", int(base.timestamp() * 1000) + 10000 + i, metric, float(i), "available")
                        for i, metric in enumerate(("cpu_percent", "rss_bytes", "disk_read_bps", "disk_write_bps"))])
    copy_db(telemetry, telemetry_backup)
    result = migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)
    assert result["sourceRows"] == 10 and result["stateSourceRows"] == 6 and result["telemetrySourceRows"] == 4 and result["importedRows"] == 10
    with sqlite3.connect(target) as db:
        metrics = {row[0] for row in db.execute("SELECT metric FROM telemetry_series")}
    assert {"cycle_avg_ms", "cycle_max_ms", "cycle_p95_ms", "rpc_avg_ms", "rpc_max_ms", "rpc_p95_ms"}.issubset(metrics)
    assert {"cpu_percent", "rss_bytes", "disk_read_bps", "disk_write_bps"}.issubset(metrics)


def test_cross_domain_identical_offset_sample_dedupes_and_conflict_refuses(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    instant = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
    make_source(source, [("minecraft", "perf.cpu_percent", "2026-08-23T08:00:00-04:00", 10.0)])
    copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    make_v1(telemetry, [("minecraft", int(instant.timestamp() * 1000), "cpu_percent", 10.0, "available")]); copy_db(telemetry, telemetry_backup)
    result = migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)
    assert result["sourceRows"] == 2 and result["stateSourceRows"] == 1 and result["telemetrySourceRows"] == 1 and result["importedRows"] == 1

    target.unlink()
    make_v1(telemetry, [("minecraft", int(instant.timestamp() * 1000), "cpu_percent", 11.0, "available")]); copy_db(telemetry, telemetry_backup)
    with pytest.raises(MigrationError, match="conflicting metric sample"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_telemetry_replay_rejects_substituted_source(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    make_v1(telemetry, [("minecraft", 1787486400000, "cpu_percent", 1.0, "available")]); copy_db(telemetry, telemetry_backup)
    migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)
    make_v1(telemetry, [("minecraft", 1787486400000, "cpu_percent", 2.0, "available")]); copy_db(telemetry, telemetry_backup)
    with pytest.raises(MigrationError, match="ledger binding"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_telemetry_source_backup_hash_mismatch_refuses(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    make_v1(telemetry, [("minecraft", 1787486400000, "cpu_percent", 1.0, "available")])
    make_v1(telemetry_backup, [("minecraft", 1787486400000, "cpu_percent", 2.0, "available")])
    with pytest.raises(MigrationError, match="telemetry backup"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


@pytest.mark.parametrize("kind", ["version", "schema", "value"])
def test_v1_malformed_version_schema_and_values_refuse(tmp_path, kind):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    if kind == "version":
        make_v1(telemetry, [], user_version=2)
    elif kind == "schema":
        make_v1(telemetry, [], exact_schema=False)
    else:
        make_v1(telemetry, [("minecraft", 1787486400000, "cpu_percent", None, "available")])
    copy_db(telemetry, telemetry_backup)
    with pytest.raises(MigrationError, match="telemetry"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_v1_telemetry_sidecar_refuses(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    make_v1(telemetry, []); copy_db(telemetry, telemetry_backup)
    telemetry.with_name(telemetry.name + "-wal").write_bytes(b"pending")
    with pytest.raises(MigrationError, match="sidecar"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_state_v3_version_and_declaration_are_exact(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)])
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=4")
    copy_db(source, backup)
    with pytest.raises(MigrationError, match="integrity"):
        migrate(source, target, backup, writer_probe=lambda: False)

    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=3")
        db.execute("DROP INDEX idx_metric_samples")
    copy_db(source, backup)
    with pytest.raises(MigrationError, match="index"):
        migrate(source, target, backup, writer_probe=lambda: False)


@pytest.mark.parametrize("kind", ["unexpected_index", "unexpected_trigger", "altered_allowed_index", "altered_allowed_trigger"])
def test_state_v3_allows_canonical_objects_but_rejects_lookalikes(tmp_path, kind):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)])
    with sqlite3.connect(source) as db:
        if kind == "unexpected_index":
            db.execute("CREATE INDEX lookalike ON events(id)")
        elif kind == "unexpected_trigger":
            db.execute("CREATE TRIGGER lookalike AFTER INSERT ON events BEGIN SELECT 1; END")
        elif kind == "altered_allowed_index":
            db.execute("DROP INDEX idx_metric_samples")
            db.execute("CREATE INDEX idx_metric_samples ON metric_samples(metric, profile_id, ts)")
        else:
            db.execute("DROP TRIGGER events_append_only_update")
            db.execute("CREATE TRIGGER events_append_only_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'changed'); END")
    copy_db(source, backup)
    with pytest.raises(MigrationError, match="state v3"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_state_source_and_backup_must_be_distinct_files(tmp_path):
    source, target = tmp_path / "state.db", tmp_path / "telemetry.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)])
    with pytest.raises(MigrationError, match="distinct"):
        migrate(source, target, source, writer_probe=lambda: False)
    hardlink = tmp_path / "hardlink.db"; hardlink.hardlink_to(source)
    with pytest.raises(MigrationError, match="insecure|distinct"):
        migrate(source, target, hardlink, writer_probe=lambda: False)


def test_target_v2_version_and_series_metadata_are_revalidated_on_replay(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    migrate(source, target, backup, writer_probe=lambda: False)
    with sqlite3.connect(target) as db:
        db.execute("PRAGMA user_version=1")
    with pytest.raises(MigrationError, match="version"):
        migrate(source, target, backup, writer_probe=lambda: False)
    with sqlite3.connect(target) as db:
        db.execute("PRAGMA user_version=2")
        db.execute("UPDATE telemetry_series SET unit='wrong'")
    with pytest.raises(MigrationError, match="metadata"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_v1_pk_and_check_contract_are_exact(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; telemetry_backup = tmp_path / "telemetry-v1-backup.db"
    with sqlite3.connect(telemetry) as db:
        db.execute("CREATE TABLE resource_samples(profile_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, metric TEXT NOT NULL CHECK (metric IN ('cpu_percent', 'rss_bytes', 'disk_read_bps', 'disk_write_bps')), value REAL CHECK (value >= 0), state TEXT NOT NULL CHECK (state IN ('available', 'inactive', 'unavailable')), PRIMARY KEY(profile_id, ts_ms, metric))")
        db.execute("CREATE INDEX resource_samples_ts ON resource_samples(ts_ms)")
        db.execute("PRAGMA user_version=1")
    telemetry.chmod(0o600)
    copy_db(telemetry, telemetry_backup)
    with pytest.raises(MigrationError, match="telemetry (schema|table definition)"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_telemetry_source_and_backup_must_be_distinct_files(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"
    make_v1(telemetry, [])
    with pytest.raises(MigrationError, match="distinct"):
        migrate(source, target, backup, telemetry_source=telemetry, telemetry_backup=telemetry, writer_probe=lambda: False)


def test_dry_run_rejects_target_same_inode_as_telemetry(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    telemetry = tmp_path / "telemetry-v1.db"; make_v1(telemetry, []); telemetry_backup = tmp_path / "telemetry-v1-backup.db"; copy_db(telemetry, telemetry_backup)
    with pytest.raises(MigrationError):
        migrate(source, telemetry, backup, dry_run=True, telemetry_source=telemetry, telemetry_backup=telemetry_backup, writer_probe=lambda: False)


def test_dry_run_rejects_existing_malformed_target(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    with sqlite3.connect(target) as db:
        db.execute("CREATE TABLE malformed(x INTEGER)"); db.execute("PRAGMA user_version=1")
    target.chmod(0o600)
    with pytest.raises(MigrationError, match="target"):
        migrate(source, target, backup, dry_run=True, writer_probe=lambda: False)


def test_late_sidecar_appearing_after_read_is_rejected(tmp_path, monkeypatch):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 20.0)]); copy_db(source, backup)
    import game_control.telemetry_migration as migration_module
    original = migration_module._fingerprint
    calls = 0
    def fingerprint(path):
        nonlocal calls
        calls += 1
        digest = original(path)
        if calls == 3:
            source.with_name(source.name + "-wal").write_bytes(b"late")
        return digest
    monkeypatch.setattr(migration_module, "_fingerprint", fingerprint)
    with pytest.raises(MigrationError, match="sidecar"):
        migrate(source, target, backup, dry_run=True, writer_probe=lambda: False)


def test_writer_guard_and_malformed_rows_fail_closed(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    backup.write_bytes(source.read_bytes() if source.exists() else b"backup"); backup.chmod(0o600)
    make_source(source, [("minecraft", "unknown", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes())
    with pytest.raises(MigrationError, match="unsupported metric"):
        migrate(source, target, backup, writer_probe=lambda: False)
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", "bad")])
    backup.write_bytes(source.read_bytes())
    with pytest.raises(MigrationError, match="malformed metric"):
        migrate(source, target, backup, writer_probe=lambda: False)
    with pytest.raises(MigrationError, match="writer"):
        migrate(source, target, backup, writer_probe=lambda: True)


def test_backup_timestamp_and_duplicate_conflicts_fail_closed(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "not-rfc3339", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    with pytest.raises(MigrationError, match="timestamp"):
        migrate(source, target, backup, writer_probe=lambda: False)
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0), ("minecraft", "tps", "2026-08-23T12:00:00Z", 2.0)])
    backup.write_bytes(source.read_bytes())
    with pytest.raises(MigrationError, match="duplicate"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_nonidentical_backup_and_insecure_target_parent_refuse(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(b"not a copy"); backup.chmod(0o600)
    with pytest.raises(MigrationError, match="exact offline"):
        migrate(source, target, backup, writer_probe=lambda: False)
    backup.write_bytes(source.read_bytes())
    insecure = tmp_path / "insecure"; insecure.mkdir(mode=0o755)
    with pytest.raises(MigrationError, match="target parent"):
        migrate(source, insecure / "telemetry.db", backup, writer_probe=lambda: False)


def test_idempotent_replay_revalidates_target_state_binding(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    migrate(source, target, backup, writer_probe=lambda: False)
    with sqlite3.connect(target) as db:
        db.execute("UPDATE telemetry_samples SET value=2")
    with pytest.raises(MigrationError, match="ledger binding|state mismatch|row binding"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_replay_allows_new_telemetry_rows_and_postcommit_fault_restores(tmp_path, monkeypatch):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    migrate(source, target, backup, writer_probe=lambda: False)
    with sqlite3.connect(target) as db:
        db.execute("INSERT INTO telemetry_samples VALUES ('resource.minecraft.tps', 999999, 2.0, 'available', 1)")
    replay = migrate(source, target, backup, writer_probe=lambda: False)
    assert replay["idempotent"] is True

    target.unlink()
    calls = 0
    original = __import__("game_control.telemetry_migration", fromlist=["_imported_state"])._imported_state
    def fault(connection, rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MigrationError("injected postcommit verification fault")
        return original(connection, rows)
    monkeypatch.setattr("game_control.telemetry_migration._imported_state", fault)
    with pytest.raises(MigrationError, match="postcommit"):
        migrate(source, target, backup, writer_probe=lambda: False)
    assert not target.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sqlite_sidecars_are_rejected_for_offline_snapshot(tmp_path, suffix):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    sidecar = source.with_name(source.name + suffix)
    sidecar.write_bytes(b"sidecar")
    with pytest.raises(MigrationError, match="sidecar"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_idempotency_ledger_binds_version_and_imported_rows(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    migrate(source, target, backup, writer_probe=lambda: False)
    with sqlite3.connect(target) as db:
        db.execute("UPDATE migration_ledger SET migration_version=99")
    with pytest.raises(MigrationError, match="ledger binding"):
        migrate(source, target, backup, writer_probe=lambda: False)

    with sqlite3.connect(target) as db:
        db.execute("UPDATE migration_ledger SET migration_version=1, imported_rows=99")
    with pytest.raises(MigrationError, match="ledger binding"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_symlink_target_is_rejected_even_when_dangling(tmp_path):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    target.symlink_to(tmp_path / "elsewhere.db")
    with pytest.raises(MigrationError, match="symlink"):
        migrate(source, target, backup, writer_probe=lambda: False)


def test_postcommit_fault_restores_existing_target_from_private_preimage(tmp_path, monkeypatch):
    source, target, backup = tmp_path / "state.db", tmp_path / "telemetry.db", tmp_path / "backup.db"
    make_source(source, [("minecraft", "tps", "2026-08-23T12:00:00Z", 1.0)])
    backup.write_bytes(source.read_bytes()); backup.chmod(0o600)
    migrate(source, target, backup, writer_probe=lambda: False)
    before = target.read_bytes()
    make_source(source, [("minecraft", "mspt", "2026-08-23T12:00:01Z", 2.0)])
    backup.write_bytes(source.read_bytes())
    calls = 0
    original = __import__("game_control.telemetry_migration", fromlist=["_imported_state"])._imported_state
    def fault(connection, rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MigrationError("injected postcommit verification fault")
        return original(connection, rows)
    monkeypatch.setattr("game_control.telemetry_migration._imported_state", fault)
    with pytest.raises(MigrationError, match="postcommit"):
        migrate(source, target, backup, writer_probe=lambda: False)
    assert target.read_bytes() == before
