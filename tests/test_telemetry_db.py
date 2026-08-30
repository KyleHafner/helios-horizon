from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from game_control.models import AdapterKind, ProfileId
from game_control.status import StatusService
from game_control.telemetry_db import RAW_RETENTION_MS, ROLLUP_RETENTION_MS, RESOURCE_METRICS, TELEMETRY_SCHEMA_VERSION, TelemetryDatabase
from game_control.telemetry_sampler import TelemetrySampler


def _sample(**overrides):
    values = {
        "cpu_percent": 12.5,
        "rss_bytes": 1024,
        "disk_read_bps": 3.0,
        "disk_write_bps": 4.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_telemetry_pragmas_schema_authorizer_and_separate_lifecycle_db(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        assert telemetry.effective_pragmas() == {"journal_mode": "wal", "synchronous": 1, "user_version": TELEMETRY_SCHEMA_VERSION}
        # The application's connection is NORMAL even though a fresh CLI
        # connection may report its own default FULL setting.
        import sqlite3

        cli = sqlite3.connect(telemetry.path)
        try:
            assert cli.execute("PRAGMA synchronous").fetchone()[0] == 2
        finally:
            cli.close()
        columns = {row[1] for row in telemetry.connection.execute("PRAGMA table_info(resource_samples)")}
        assert {"profile_id", "ts_ms", "metric", "value", "state"} <= columns
        assert telemetry.path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(Exception):
            telemetry.connection.execute("ATTACH DATABASE ':memory:' AS forbidden")
    finally:
        telemetry.close()


def test_schema_v2_registry_rollup_and_expected_state(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        assert telemetry.retention_ms == RAW_RETENTION_MS
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=1000, state="available")
        series = telemetry.connection.execute("SELECT series_id, profile_id, metric FROM telemetry_series ORDER BY metric").fetchall()
        assert len(series) == 4
        assert all(row[0] == f"resource.minecraft.{row[2]}" for row in series)
        assert telemetry.connection.execute("SELECT DISTINCT expected FROM resource_samples").fetchall() == [(1,)]
        telemetry.record_rollup("minecraft", "cpu_percent", bucket_start_ms=0, minimum=1, maximum=3, total=4, count=2)
        telemetry.record_rollup("minecraft", "cpu_percent", bucket_start_ms=0, minimum=0, maximum=5, total=6, count=1)
        assert telemetry.connection.execute("SELECT min, max, sum, count FROM telemetry_rollups").fetchone() == (0.0, 5.0, 10.0, 3)
        assert "p95" not in {row[1] for row in telemetry.connection.execute("PRAGMA table_info(telemetry_rollups)")}
    finally:
        telemetry.close()


def test_v2_reopen_and_sql_state_invariants(tmp_path: Path):
    path = tmp_path / "telemetry.db"
    first = TelemetryDatabase.open(path)
    first.record_process_sample("minecraft", _sample(), ts_ms=1, state="unavailable")
    first.close()
    telemetry = TelemetryDatabase.open(path)
    try:
        row = telemetry.connection.execute("SELECT state, value, expected FROM resource_samples LIMIT 1").fetchone()
        assert row == ("unavailable", None, 1)
        invalid = [
            ("resource.minecraft.cpu_percent", "minecraft", 2, "cpu_percent", 1, "inactive", 0),
            ("resource.minecraft.cpu_percent", "minecraft", 3, "cpu_percent", 1, "unavailable", 1),
            ("resource.minecraft.cpu_percent", "minecraft", 4, "cpu_percent", None, "available", 1),
        ]
        for values in invalid:
            with pytest.raises(Exception):
                telemetry.connection.execute("INSERT INTO resource_samples VALUES (?, ?, ?, ?, ?, ?, ?)", values)
    finally:
        telemetry.close()


def test_v1_without_legacy_table_is_rejected(tmp_path: Path):
    import sqlite3
    path = tmp_path / "telemetry.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 1"); connection.commit(); connection.close()
    with pytest.raises(RuntimeError):
        TelemetryDatabase.open(path)


def test_metric_neutral_registry_and_typed_queries(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        telemetry.record_sample("minecraft", "tps", 19.8, ts_ms=100)
        telemetry.record_sample("minecraft", "players", 0, ts_ms=100, state="inactive")
        telemetry.record_sample("minecraft", "gc_pause", 12.0, ts_ms=100)
        telemetry.record_sample("minecraft", "tick_histogram_le_10", 4, ts_ms=100, labels={"bucket": "10"})
        rows = telemetry.query_samples("minecraft", "tps")
        assert rows == [(100, 19.8, "available")]
        registry = telemetry.connection.execute("SELECT metric, unit, kind FROM telemetry_series ORDER BY metric").fetchall()
        assert ("tps", "tps", "gauge") in registry
        assert ("gc_pause", "milliseconds", "observation") in registry
        assert ("tick_histogram_le_10", "ticks", "histogram") in registry
        telemetry.record_rollup("minecraft", "tick_histogram_le_10", bucket_start_ms=0, bucket=10, minimum=1, maximum=4, total=5, count=2)
        assert telemetry.connection.execute("""SELECT r.labels_json,u.count FROM telemetry_rollups u
            JOIN telemetry_series r USING(series_id)""").fetchone() == ('{"bucket":"10"}', 2)
    finally:
        telemetry.close()


def test_normalized_samples_labels_and_metadata_do_not_drift(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        labels = {"source": "prometheus", "bucket": "+Inf"}
        telemetry.record_sample("minecraft", "tps", 20, ts_ms=1, labels=labels)
        telemetry.record_sample("minecraft", "tps", 19, ts_ms=1, labels={"source": "rcon", "bucket": "10"})
        with pytest.raises(ValueError):
            telemetry.record_sample("minecraft", "tps", 20, ts_ms=2, labels={"source": "player-name"})
        columns = {row[1] for row in telemetry.connection.execute("PRAGMA table_info(telemetry_samples)")}
        assert columns == {"series_id", "ts_ms", "value", "state", "expected"}
        assert telemetry.connection.execute("SELECT count(*) FROM telemetry_samples").fetchone()[0] == 2
        assert telemetry.query_samples("minecraft", "tps", labels=labels) == [(1, 20.0, "available")]
        metadata = telemetry.connection.execute("SELECT labels_json, series_id FROM telemetry_series WHERE metric='tps' ORDER BY labels_json").fetchall()
        assert len(metadata) == 2 and metadata[0][0] != metadata[1][0]
        assert all('.l' in row[1] and '{' not in row[1] for row in metadata)
    finally:
        telemetry.close()


def test_schema_v2_rejects_invalid_values_and_keeps_retention_boundary(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", retention_ms=60_000)
    try:
        with pytest.raises(ValueError):
            telemetry.record_process_sample("minecraft", _sample(cpu_percent=float("nan")), ts_ms=1, state="available")
        with pytest.raises(ValueError):
            telemetry.record_process_sample("minecraft", _sample(), ts_ms=True, state="available")
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=100_000, state="available")
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=40_000, state="available")
        assert telemetry.connection.execute("SELECT min(ts_ms) FROM resource_samples").fetchone()[0] == 40_000
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=160_000, state="available")
        assert telemetry.connection.execute("SELECT min(ts_ms) FROM resource_samples").fetchone()[0] == 100_000
    finally:
        telemetry.close()


def test_schema_v2_migrates_v1_rows_transactionally(tmp_path: Path):
    import sqlite3
    path = tmp_path / "telemetry.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE resource_samples(profile_id TEXT, ts_ms INTEGER, metric TEXT, value REAL, state TEXT, PRIMARY KEY(profile_id, ts_ms, metric))")
    connection.execute("INSERT INTO resource_samples VALUES ('minecraft', 10, 'cpu_percent', 1.5, 'available')")
    connection.execute("INSERT INTO resource_samples VALUES ('minecraft', 20, 'cpu_percent', 2.5, 'available')")
    connection.execute("PRAGMA user_version = 1")
    connection.commit(); connection.close()
    telemetry = TelemetryDatabase.open(path)
    try:
        assert telemetry.effective_pragmas()["user_version"] == 2
        assert telemetry.connection.execute("SELECT series_id, expected FROM resource_samples").fetchone() == ("resource.minecraft.cpu_percent", 1)
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 2
    finally:
        telemetry.close()


def test_v2_foreign_key_and_state_value_invariants(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        with pytest.raises(Exception):
            telemetry.connection.execute("INSERT INTO resource_samples VALUES ('missing', 'minecraft', 1, 'cpu_percent', 1, 'available', 1)")
        with pytest.raises(Exception):
            telemetry.connection.execute("INSERT INTO resource_samples VALUES ('resource.minecraft.cpu_percent', 'minecraft', 1, 'cpu_percent', 1, 'inactive', 0)")
        telemetry.record_process_sample("minecraft", SimpleNamespace(cpu_percent=None), ts_ms=1, state="available")
        states = telemetry.connection.execute("SELECT state, value, expected FROM resource_samples").fetchall()
        assert states and all(state == "unavailable" and value is None and expected == 1 for state, value, expected in states)
    finally:
        telemetry.close()


def test_v1_migration_failure_rolls_back_without_partial_schema(tmp_path: Path):
    import sqlite3
    path = tmp_path / "telemetry.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE resource_samples(profile_id TEXT, ts_ms INTEGER, metric TEXT, value REAL, state TEXT)")
    connection.execute("INSERT INTO resource_samples VALUES ('minecraft', 1, 'unknown', 1, 'available')")
    connection.execute("PRAGMA user_version = 1"); connection.commit(); connection.close()
    with pytest.raises(RuntimeError):
        TelemetryDatabase.open(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='resource_samples'").fetchone() is not None
    assert connection.execute("SELECT name FROM sqlite_master WHERE name='resource_samples_legacy'").fetchone() is None
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'telemetry_%'")}
    assert names == set()
    assert connection.execute("SELECT * FROM resource_samples").fetchall() == [('minecraft', 1, 'unknown', 1.0, 'available')]
    connection.close()


def test_v2_requires_canonical_tables_not_legacy_shape(tmp_path: Path):
    import sqlite3
    path = tmp_path / "telemetry.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE telemetry_series(series_id TEXT PRIMARY KEY,profile_id TEXT,metric TEXT,unit TEXT,kind TEXT,labels_json TEXT,active INTEGER)")
    connection.execute("CREATE TABLE telemetry_samples(series_id TEXT,ts_ms INTEGER,value REAL,state TEXT,expected INTEGER)")
    connection.execute("CREATE TABLE telemetry_rollups(series_id TEXT,bucket_start_ms INTEGER,min REAL,max REAL,sum REAL,count INTEGER)")
    connection.execute("CREATE VIEW resource_samples AS SELECT 1")
    connection.execute("PRAGMA user_version=2")
    connection.commit(); connection.close()
    with pytest.raises(RuntimeError, match="malformed telemetry schema"):
        TelemetryDatabase.open(path)


def test_series_collision_and_invalid_query_labels_fail_closed(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        telemetry.record_sample("minecraft", "tps", 20, ts_ms=1)
        telemetry.connection.execute("UPDATE telemetry_series SET unit='drifted' WHERE metric='tps'")
        telemetry.connection.commit()
        with pytest.raises(RuntimeError, match="metadata collision"):
            telemetry.record_sample("minecraft", "tps", 19, ts_ms=2)
        with pytest.raises(RuntimeError, match="metadata collision"):
            telemetry.query_samples("minecraft", "tps")
        for labels in ({"player": "alice"}, {"source": "invalid"}, {"bucket": "NaN"}):
            with pytest.raises(ValueError):
                telemetry.query_samples("minecraft", "tps", labels=labels)
        with pytest.raises(ValueError):
            telemetry.query_samples("bad profile", "tps")
        with pytest.raises(ValueError):
            telemetry.query_samples("minecraft", "tps", since_ms=-1)
    finally:
        telemetry.close()


def test_decimal_and_inf_histogram_rollups_are_label_normalized(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        telemetry.record_rollup("minecraft", "tick_histogram_le_10", bucket_start_ms=0,
                                bucket="0.005", minimum=1, maximum=2, total=3, count=2)
        telemetry.record_rollup("minecraft", "tick_histogram_le_10", bucket_start_ms=0,
                                bucket="+Inf", minimum=2, maximum=4, total=6, count=2)
        rows = telemetry.connection.execute("""SELECT s.labels_json,r.min,r.max,r.sum,r.count
            FROM telemetry_rollups r JOIN telemetry_series s USING(series_id) ORDER BY s.labels_json""").fetchall()
        assert rows == [('{"bucket":"+Inf"}', 2.0, 4.0, 6.0, 2), ('{"bucket":"0.005"}', 1.0, 2.0, 3.0, 2)]
        assert "bucket" not in {row[1] for row in telemetry.connection.execute("PRAGMA table_info(telemetry_rollups)")}
    finally:
        telemetry.close()


def test_hourly_compaction_is_idempotent_and_enforces_both_retentions(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", retention_ms=60_000)
    try:
        telemetry.record_sample("minecraft", "tps", 18, ts_ms=1_000)
        telemetry.record_sample("minecraft", "tps", 20, ts_ms=2_000)
        telemetry.record_sample("minecraft", "tick_histogram_le_10", 3, ts_ms=1_000,
                                labels={"source": "prometheus", "bucket": "0.01"})
        first = telemetry.compact_hourly(now_ms=70_000)
        assert first["raw_deleted"] == 3
        scalar = telemetry.connection.execute("""SELECT min,max,sum,count FROM telemetry_rollups r
            JOIN telemetry_series s USING(series_id) WHERE s.metric='tps'""").fetchone()
        assert scalar == (18.0, 20.0, 38.0, 2)
        histogram = telemetry.connection.execute("""SELECT s.labels_json,r.sum,r.count FROM telemetry_rollups r
            JOIN telemetry_series s USING(series_id) WHERE s.kind='histogram'""").fetchone()
        assert histogram == ('{"bucket":"0.01","source":"prometheus"}', 3.0, 1)
        before = telemetry.connection.execute("SELECT * FROM telemetry_rollups ORDER BY series_id").fetchall()
        assert telemetry.compact_hourly(now_ms=70_000)["hours_compacted"] == 0
        assert telemetry.connection.execute("SELECT * FROM telemetry_rollups ORDER BY series_id").fetchall() == before
        columns = {row[1] for row in telemetry.connection.execute("PRAGMA table_info(telemetry_rollups)")}
        assert "p95" not in columns
        telemetry.compact_hourly(now_ms=ROLLUP_RETENTION_MS + 1)
        assert telemetry.connection.execute("SELECT count(*) FROM telemetry_rollups").fetchone()[0] == 0
    finally:
        telemetry.close()


def test_counter_compaction_uses_reset_aware_deltas_and_breaks_on_inactive_gap(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", retention_ms=60_000)
    metric = "host_network_rx_bytes_total"
    try:
        for ts_ms, value, state in (
            (0, 100, "available"), (1_000, 130, "available"),
            (2_000, None, "inactive"), (3_000, 200, "available"),
            (4_000, 230, "available"), (3_599_000, 240, "available"),
            (3_601_000, 260, "available"), (3_602_000, 5, "available"),
        ):
            telemetry.record_sample("minecraft", metric, value, ts_ms=ts_ms, state=state)
        telemetry.compact_hourly(now_ms=7_300_000)
        rows = telemetry.connection.execute(
            """SELECT r.bucket_start_ms,r.min,r.max,r.sum,r.count FROM telemetry_rollups r
               JOIN telemetry_series s USING(series_id) WHERE s.metric=? ORDER BY r.bucket_start_ms""",
            (metric,),
        ).fetchall()
        assert rows == [(0, 10.0, 30.0, 70.0, 3), (3_600_000, 5.0, 20.0, 25.0, 2)]
        assert sum(row[3] for row in rows) == 95.0
        assert telemetry.connection.execute(
            "SELECT count(*) FROM telemetry_samples x JOIN telemetry_series s USING(series_id) WHERE s.metric=?",
            (metric,),
        ).fetchone()[0] == 0
    finally:
        telemetry.close()


def test_telemetry_rejects_symlink_target_and_unsecured_parent(tmp_path: Path):
    target = tmp_path / "telemetry.db"
    real = tmp_path / "real.db"
    real.touch()
    target.symlink_to(real)
    with pytest.raises(PermissionError):
        TelemetryDatabase.open(target)
    with pytest.raises(PermissionError):
        TelemetryDatabase.open(tmp_path / "missing" / "telemetry.db")


def test_telemetry_rejects_insecure_parent_and_sidecars(tmp_path: Path):
    parent = tmp_path / "insecure"
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(PermissionError):
        TelemetryDatabase.open(parent / "telemetry.db")
    parent.chmod(0o755)
    sidecar = parent / "telemetry.db-wal"
    sidecar.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PermissionError):
        TelemetryDatabase.open(parent / "telemetry.db")


def test_telemetry_upsert_whitelist_and_retention(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", retention_ms=60_000)
    try:
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=100_000, state="available")
        telemetry.record_process_sample("minecraft", _sample(cpu_percent=20), ts_ms=100_000, state="available")
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 4
        assert telemetry.connection.execute(
            "SELECT value FROM resource_samples WHERE profile_id='minecraft' AND metric='cpu_percent'"
        ).fetchone()[0] == 20
        telemetry.record_process_sample("minecraft", _sample(), ts_ms=200_000, state="available")
        assert telemetry.connection.execute("SELECT min(ts_ms) FROM resource_samples").fetchone()[0] == 200_000
        assert set(RESOURCE_METRICS) == {row[0] for row in telemetry.connection.execute("SELECT DISTINCT metric FROM resource_samples")}
    finally:
        telemetry.close()


def test_integrated_sampler_storage_cadence_contract_is_five_seconds() -> None:
    sampler = TelemetrySampler(lambda: None)
    assert sampler.interval_seconds == 5.0


@pytest.mark.asyncio
async def test_status_telemetry_failure_does_not_break_availability(tmp_path: Path):
    class BrokenTelemetry:
        def record_process_sample(self, *_args, **_kwargs):
            raise OSError("telemetry unavailable")

        def record_failure(self, error):
            self.error = error

        def health(self):
            return {"ok": False, "last_sample_age_ms": None, "last_error": "OSError"}

    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    service = StatusService(
        [profile], adapters={"minecraft": SimpleNamespace(observe=lambda _p: SimpleNamespace(running=False, healthy=None))},
        telemetry_db=BrokenTelemetry(), clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    snapshot = await service.snapshot(persist=True)
    assert snapshot.profiles[0].state.value == "stopped"
    assert service.telemetry_health()["ok"] is False


@pytest.mark.asyncio
async def test_inactive_samples_are_explicit_not_zero(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    service = StatusService(
        [profile], slot_observer=lambda: SimpleNamespace(owner=ProfileId.PZ_RISING), telemetry_db=telemetry,
        clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    try:
        await service.snapshot(persist=True)
        assert telemetry.drain(timeout=2)
        rows = telemetry.connection.execute("SELECT value,state FROM resource_samples").fetchall()
        assert len(rows) == 4
        assert all(value is None and state == "inactive" for value, state in rows)
    finally:
        telemetry.close()


@pytest.mark.asyncio
async def test_refresh_snapshot_is_non_persistent_until_maintenance_mode(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    store = SimpleNamespace(records=[], stops=[])
    store.record = lambda *args, **kwargs: store.records.append((args, kwargs))
    store.profile_stopped = lambda *args, **kwargs: store.stops.append((args, kwargs))
    service = StatusService(
        [profile],
        adapter=SimpleNamespace(observe=lambda _p: SimpleNamespace(running=True, healthy=True, pid=7)),
        metrics=SimpleNamespace(sample=lambda _p, **_k: _sample()),
        session_store=store,
        telemetry_db=telemetry,
        clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    try:
        await service.snapshot()
        await service.snapshot()
        assert store.records == []
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 0
        await service.snapshot(persist=True)
        assert telemetry.drain(timeout=2)
        assert len(store.records) == 1
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 4
    finally:
        telemetry.close()


@pytest.mark.asyncio
async def test_nonpersistent_inactive_refresh_does_not_write_telemetry(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    service = StatusService(
        [profile],
        slot_observer=lambda: SimpleNamespace(owner=ProfileId.PZ_RISING),
        telemetry_db=telemetry,
        clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    try:
        await service.snapshot()
        await service.snapshot()
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 0
        await service.snapshot(persist=True)
        assert telemetry.drain(timeout=2)
        assert telemetry.connection.execute("SELECT count(*) FROM resource_samples").fetchone()[0] == 4
    finally:
        telemetry.close()


@pytest.mark.asyncio
async def test_slow_telemetry_writer_does_not_block_concurrent_snapshot(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    entered = threading.Event()
    release = threading.Event()
    original = telemetry._write_with

    def slow_write(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)

    telemetry._write_with = slow_write
    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    service = StatusService(
        [profile], adapters={"minecraft": SimpleNamespace(observe=lambda _p: SimpleNamespace(running=False, healthy=None))},
        telemetry_db=telemetry, clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    try:
        await service.snapshot(persist=True)
        assert entered.wait(timeout=2)
        started = time.monotonic()
        snapshot = await service.snapshot()
        assert time.monotonic() - started < 0.2
        assert snapshot.profiles[0].state.value == "stopped"
    finally:
        release.set()
        telemetry.close()


def test_telemetry_queue_is_bounded_and_coalesces(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", queue_size=1)
    entered = threading.Event()
    release = threading.Event()
    original = telemetry._write_with

    def slow_write(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)

    telemetry._write_with = slow_write
    try:
        assert telemetry.enqueue_process_sample("one", _sample(), ts_ms=1, state="available")
        assert entered.wait(timeout=2)
        assert telemetry.enqueue_process_sample("one", _sample(cpu_percent=22), ts_ms=2, state="available")
        assert telemetry.enqueue_process_sample("two", _sample(), ts_ms=3, state="available") is False
        health = telemetry.health()
        assert health["pending"] <= 1
        assert health["coalesced"] == 1
        assert health["dropped"] == 1
        assert health["ok"] is False
    finally:
        release.set()
        telemetry.close()


def test_generic_enqueue_is_nonblocking_coalesced_ordered_and_bounded(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db", queue_size=1)
    entered = threading.Event()
    release = threading.Event()
    original = telemetry._write_generic_with

    def slow_write(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        return original(*args, **kwargs)

    telemetry._write_generic_with = slow_write
    try:
        assert telemetry.enqueue_sample("minecraft", "mspt_p95", 10, ts_ms=1,
                                        labels={"source": "prometheus"})
        assert entered.wait(timeout=2)
        assert telemetry.enqueue_sample("minecraft", "mspt_p95", 30, ts_ms=3,
                                        labels={"source": "prometheus"})
        assert telemetry.enqueue_sample("minecraft", "mspt_p95", 20, ts_ms=2,
                                        labels={"source": "prometheus"})
        assert telemetry.enqueue_sample("minecraft", "tps", 20, ts_ms=3) is False
        release.set()
        assert telemetry.drain(timeout=2)
        assert telemetry.query_samples("minecraft", "mspt_p95", labels={"source": "prometheus"}) == [
            (1, 10.0, "available"), (3, 30.0, "available")
        ]
        assert telemetry.health()["coalesced"] == 2
        assert telemetry.health()["dropped"] == 1
    finally:
        release.set()
        telemetry.close()
    assert telemetry.enqueue_sample("minecraft", "tps", 20, ts_ms=4) is False


def test_generic_enqueue_concurrency_keeps_latest_timestamp(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        threads = [threading.Thread(
            target=telemetry.enqueue_sample,
            args=("minecraft", "tick_ms_bucket", index),
            kwargs={"ts_ms": index, "labels": {"source": "prometheus", "bucket": "+Inf"}},
        ) for index in range(1, 17)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert telemetry.drain(timeout=2)
        rows = telemetry.query_samples("minecraft", "tick_ms_bucket",
                                       labels={"source": "prometheus", "bucket": "+Inf"})
        assert rows[-1] == (16, 16.0, "available")
        assert [row[0] for row in rows] == sorted(row[0] for row in rows)
    finally:
        telemetry.close()


def test_generic_enqueue_fixed_slo_result_allowlist(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    try:
        assert telemetry.enqueue_sample("minecraft", "wake_duration", 12, ts_ms=1,
                                        labels={"source": "slotd", "result": "success"})
        with pytest.raises(ValueError, match="result"):
            telemetry.enqueue_sample("minecraft", "wake_duration", 12, ts_ms=2,
                                     labels={"result": "custom text"})
        assert telemetry.drain(timeout=2)
    finally:
        telemetry.close()


def test_host_source_metric_registry_accepts_all_emitted_io_fields(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    metrics = {
        "host_psi_io_some_avg10": 0.1,
        "host_psi_io_full_avg10": 0.0,
        "host_disk_read_io_time_ms_total": 10,
        "host_disk_write_io_time_ms_total": 20,
        "service_io_read_bytes_total": 30,
        "service_io_write_bytes_total": 40,
        "service_io_read_ops_total": 5,
        "service_io_write_ops_total": 6,
        "host_network_rx_bytes_total": 50,
        "host_network_tx_bytes_total": 60,
    }
    try:
        assert all(telemetry.enqueue_sample("minecraft", metric, value, ts_ms=1)
                   for metric, value in metrics.items())
        assert telemetry.drain(timeout=2)
        stored = {row[0] for row in telemetry.connection.execute("SELECT metric FROM telemetry_series")}
        assert stored == set(metrics)
    finally:
        telemetry.close()


def test_telemetry_writer_failure_is_reported_and_close_is_idempotent(tmp_path: Path):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")

    def broken_write(*_args, **_kwargs):
        raise OSError("disk full")

    telemetry._write_with = broken_write
    assert telemetry.enqueue_process_sample("minecraft", _sample(), ts_ms=1, state="available")
    assert telemetry.drain(timeout=2)
    assert telemetry.health()["ok"] is False
    assert telemetry.health()["last_error"] == "OSError"
    telemetry.close()
    telemetry.close()


def test_writer_startup_failure_is_terminal_and_enqueue_rejected(tmp_path: Path, monkeypatch):
    import sqlite3
    original_connect = sqlite3.connect
    calls = 0

    def fail_worker_connect(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("writer startup failed")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_worker_connect)
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    assert telemetry._terminal_event.wait(timeout=2)
    assert telemetry.enqueue_process_sample("minecraft", _sample(), ts_ms=1, state="available") is False
    health = telemetry.health()
    assert health["state"] == "failed"
    assert health["last_error"] == "OSError"
    result = telemetry.close()
    assert result["closed"] is True
    assert telemetry.close() == result


def test_close_timeout_is_bounded_and_reports_abandoned(tmp_path: Path, monkeypatch):
    telemetry = TelemetryDatabase.open(tmp_path / "telemetry.db")
    assert telemetry.enqueue_process_sample("minecraft", _sample(), ts_ms=1, state="available")
    monkeypatch.setattr(telemetry, "drain", lambda timeout=None: False)
    result = telemetry.close()
    assert result["closed"] is True
    assert result["drained"] is False
    assert result["abandoned"] is True
    assert telemetry.health()["abandoned"] >= 1
    # Terminal reconciliation must leave queue.join bookkeeping settled.
    telemetry._queue.join()


@pytest.mark.asyncio
async def test_status_retries_cadence_after_enqueue_rejection():
    calls = []

    class RejectingTelemetry:
        def enqueue_process_sample(self, *args, **kwargs):
            calls.append((args, kwargs))
            return False

        def record_failure(self, error):
            self.error = error

        def health(self):
            return {"ok": False, "last_error": "Backpressure"}

    profile = SimpleNamespace(id="minecraft", adapter=AdapterKind.CRAFTY, systemd_unit=None, paths=SimpleNamespace(version_file="/missing"))
    service = StatusService(
        [profile], adapters={"minecraft": SimpleNamespace(observe=lambda _p: SimpleNamespace(running=False, healthy=None))},
        telemetry_db=RejectingTelemetry(), monotonic=iter((0.0, 1.0)).__next__,
        clock=lambda: datetime.fromtimestamp(2, timezone.utc),
    )
    await service.snapshot(persist=True)
    await service.snapshot(persist=True)
    assert len(calls) == 2
