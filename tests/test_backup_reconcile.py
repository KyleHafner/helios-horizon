from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_control.backup_reconcile import (
    RETAINED_PROFILE_IDS,
    CatalogGeneration,
    PruneCandidate,
    ProtectionGeneration,
    ProtectedFlagCorrection,
    ReconciliationPlan,
    RemoteGeneration,
    MAX_MANIFEST_BYTES,
    _download_and_verify_manifest,
    _read_manifest,
    build_fixed_plan,
    apply_replacement_plan,
    apply_plan,
    plan_json,
    reconcile,
)
from game_control.backups import RemoteObject, b2_prefix
from game_control.errors import SafeError


def test_installed_reconcile_helper_uses_fixed_package_venv():
    helper = (Path(__file__).parents[1] / "ops/bin/horizon-backup-reconcile").read_text()
    assert helper.startswith("#!/opt/game-control/.venv/bin/python\n")


def test_zstd_manifest_fallback_reads_only_cap_plus_one(tmp_path, monkeypatch):
    path = tmp_path / "evidence.tar.zst"
    path.write_bytes(b"not-a-tarfile")
    observed = {}
    observed["commands"] = []

    class _Stream:
        def read(self, size):
            observed["read_size"] = size
            return b"x" * size

    class _Process:
        args = ["/usr/bin/tar"]
        stdout = _Stream()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def kill(self):
            observed["killed"] = True

        def wait(self, timeout=None):
            observed["timeout"] = timeout
            return -9

    monkeypatch.setattr("game_control.maintenance_process.active_block_schedulers", lambda: ("none",))

    def popen(argv, *args, **kwargs):
        observed["commands"].append(argv)
        return _Process()

    monkeypatch.setattr("game_control.backup_reconcile.subprocess.Popen", popen)
    with pytest.raises(SafeError, match="manifest exceeds bound"):
        _read_manifest(path)
    assert observed["commands"][0][:6] == ["/usr/bin/systemd-run", "--wait", "--pipe", "--quiet", "--service-type=exec", "--slice=maintenance.slice"]
    assert "/usr/bin/tar" in observed["commands"][0]
    assert observed["read_size"] == MAX_MANIFEST_BYTES + 1
    assert observed["killed"] is True
    assert observed["timeout"] == 60


def test_partial_remote_prune_recovers_exact_newest_two_and_is_idempotent():
    profile_id = "terraria-vanilla"
    older = _catalog(profile_id, 1, protected=True)
    scheduled = _catalog(profile_id, 2, protected=False)
    replacement = _catalog(profile_id, 3, protected=True)
    remote = (_remote(scheduled), _remote(replacement))
    protections = (
        ProtectionGeneration(
            backup_id=older.backup_id,
            profile_id=profile_id,
            remote_key=f"{b2_prefix(profile_id)}/{older.backup_id}.tar.zst",
            local_sha256=older.sha256,
            local_verified=True,
            remote_verified=True,
            comparison_state="verified",
        ),
        ProtectionGeneration(
            backup_id=scheduled.backup_id,
            profile_id=profile_id,
            remote_key=remote[0].remote_key,
            local_sha256=scheduled.sha256,
            local_verified=True,
            remote_verified=True,
            comparison_state="verified",
        ),
    )

    plan = reconcile((older, scheduled, replacement), protections, remote, replacement_mode=True)
    assert plan.ready
    assert plan.prune_candidates == ()
    assert [item.backup_id for item in plan.pruned_protection_corrections] == [older.backup_id]
    assert [item.backup_id for item in plan.imports] == [replacement.backup_id]
    assert plan.protected_flag_corrections == (
        ProtectedFlagCorrection(profile_id, older.backup_id, False),
        ProtectedFlagCorrection(profile_id, scheduled.backup_id, True),
    )

    connection = _database((older, scheduled, replacement), protections)

    class _Transport:
        def __init__(self):
            self.objects = {item.remote_key: item.size_bytes for item in remote}

        def list(self, prefix):
            return tuple(
                RemoteObject(key, size)
                for key, size in self.objects.items()
                if key.startswith(prefix + "/")
            )

        def delete(self, key):
            raise AssertionError(f"already-pruned remote must not be deleted again: {key}")

    apply_replacement_plan(connection, _Transport(), plan)
    assert connection.execute(
        "SELECT prune_state FROM backup_protections WHERE backup_id=?", (older.backup_id,)
    ).fetchone()[0] == "deleted"
    assert connection.execute(
        "SELECT COUNT(*) FROM backup_protections WHERE backup_id=?", (replacement.backup_id,)
    ).fetchone()[0] == 1

    active = tuple(
        ProtectionGeneration(
            backup_id=row[0], profile_id=row[1], remote_key=row[2], local_sha256=row[3],
            local_verified=bool(row[4]), remote_verified=bool(row[5]), comparison_state=row[6],
            prune_state=row[7], upload_state=row[8], error_code=row[9],
        )
        for row in connection.execute(
            "SELECT backup_id,profile_id,remote_key,local_sha256,local_verified,remote_verified,"
            "comparison_state,prune_state,upload_state,error_code FROM backup_protections"
        )
    )
    second_catalog = (
        CatalogGeneration(**{**older.__dict__, "protected": False}),
        CatalogGeneration(**{**scheduled.__dict__, "protected": True}),
        replacement,
    )
    second = reconcile(second_catalog, active, remote, replacement_mode=True)
    assert second.ready
    assert second.imports == ()
    assert second.prune_candidates == ()


def test_completed_remote_prune_clears_stale_catalog_protection_idempotently():
    profile_id = "minecraft-sunlit-cobblemon"
    older, retained_one, retained_two = (
        _catalog(profile_id, index, protected=True) for index in (1, 2, 3)
    )
    deleted = ProtectionGeneration(
        backup_id=older.backup_id,
        profile_id=profile_id,
        remote_key=f"{b2_prefix(profile_id)}/{older.backup_id}.tar.zst",
        local_sha256=older.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state="deleted",
    )
    active = tuple(
        ProtectionGeneration(
            backup_id=item.backup_id,
            profile_id=profile_id,
            remote_key=f"{b2_prefix(profile_id)}/{item.backup_id}.tar.zst",
            local_sha256=item.sha256,
            local_verified=True,
            remote_verified=True,
            comparison_state="verified",
        )
        for item in (retained_one, retained_two)
    )
    remote = (_remote(retained_one), _remote(retained_two))

    plan = reconcile(
        (older, retained_one, retained_two),
        (deleted, *active),
        remote,
        replacement_mode=True,
    )

    assert plan.ready
    assert plan.prune_candidates == ()
    assert plan.protected_flag_corrections == (
        ProtectedFlagCorrection(profile_id, older.backup_id, False),
    )
    connection = _database((older, retained_one, retained_two), (deleted, *active))
    apply_plan(connection, plan)
    assert connection.execute(
        "SELECT protected FROM backups WHERE id=?", (older.backup_id,)
    ).fetchone()[0] == 0
    second_catalog, second_protections = _captured(connection)
    second = reconcile(second_catalog, second_protections, remote, replacement_mode=True)
    assert second.ready
    assert second.protected_flag_corrections == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("local_sha256", "0" * 64),
        ("remote_key", "noncanonical/object.tar.zst"),
        ("remote_verified", False),
        ("comparison_state", "mismatch"),
        ("upload_state", "failed"),
        ("error_code", "prune_failed"),
    ),
)
def test_completed_remote_prune_refuses_unproven_deleted_state(field, value):
    item = _catalog("minecraft-sunlit-cobblemon", 1, protected=True)
    clean = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state="deleted",
    )
    deleted = ProtectionGeneration(**{**clean.__dict__, field: value})

    plan = reconcile((item,), (deleted,), (), replacement_mode=True)

    assert not plan.ready
    assert "deleted protection state is not proven" in plan.refusals
    assert plan.protected_flag_corrections == ()


def test_completed_remote_prune_refuses_remote_reappearance():
    item = _catalog("minecraft-sunlit-cobblemon", 1, protected=False)
    deleted = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state="deleted",
    )

    plan = reconcile((item,), (deleted,), (_remote(item),), replacement_mode=True)

    assert not plan.ready
    assert "deleted protection has matching remote generation" in plan.refusals
    assert plan.imports == ()


def test_deleted_protection_without_catalog_generation_refuses():
    item = _catalog("minecraft-sunlit-cobblemon", 1)
    deleted = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state="deleted",
    )

    plan = reconcile((), (deleted,), (), replacement_mode=True)

    assert not plan.ready
    assert "deleted protection row has no catalog generation" in plan.refusals
    assert plan.protected_flag_corrections == ()


def _backup_id(index: int) -> str:
    return f"20260805T000000000000Z-{index:012x}"


def _catalog(profile_id: str, index: int, *, protected: bool = True) -> CatalogGeneration:
    backup_id = _backup_id(index)
    size = len(f"{profile_id}:{backup_id}")
    digest = hashlib.sha256(f"{profile_id}:{backup_id}".encode()).hexdigest()
    return CatalogGeneration(
        backup_id=backup_id,
        profile_id=profile_id,
        size_bytes=size,
        archive_size_bytes=size,
        sha256=digest,
        verified=True,
        protected=protected,
    )


def _remote(item: CatalogGeneration, *, key: str | None = None, size: int | None = None) -> RemoteGeneration:
    return RemoteGeneration(
        profile_id=item.profile_id,
        backup_id=item.backup_id,
        size_bytes=item.size_bytes if size is None else size,
        sha256=item.sha256,
        remote_key=key or f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
    )


def _orphan(profile_id: str, index: int) -> RemoteGeneration:
    backup_id = f"20240804T000000000000Z-{index:012x}"
    size = 100 + index
    return RemoteGeneration(
        profile_id=profile_id,
        backup_id=backup_id,
        size_bytes=size,
        sha256=("a" if index % 2 else "b") * 64,
        remote_key=f"{b2_prefix(profile_id)}/{backup_id}.tar.zst",
        manifest_profile_id=profile_id,
        manifest_backup_id=backup_id,
        manifest_verified=True,
        downloaded_size_bytes=size,
        verification_method="manifest",
    )


def _protection(item: CatalogGeneration, *, prune_state: str = "not_started") -> ProtectionGeneration:
    return ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state=prune_state,
    )


def _observed_322_fixture():
    catalog = []
    protections = []
    remote = []
    expected_replacements = []
    expected_already_pruned = []
    for offset, profile_id in enumerate(RETAINED_PROFILE_IDS):
        base = 10 * (offset + 1)
        if profile_id == "minecraft-sunlit-cobblemon":
            older, retained_one, retained_two = (
                _catalog(profile_id, base + index) for index in range(3)
            )
            catalog.extend((older, retained_one, retained_two))
            protections.extend(_protection(item) for item in (older, retained_one, retained_two))
            remote.extend((_remote(retained_one), _remote(retained_two), _orphan(profile_id, base)))
            expected_already_pruned.append(older.backup_id)
        else:
            retained_one, retained_two = (
                _catalog(profile_id, base + index) for index in range(2)
            )
            catalog.extend((retained_one, retained_two))
            protections.extend(_protection(item) for item in (retained_one, retained_two))
            remote.extend((_remote(retained_one), _orphan(profile_id, base)))
            expected_replacements.append(retained_two.backup_id)
    return (
        tuple(catalog),
        tuple(protections),
        tuple(remote),
        expected_replacements,
        expected_already_pruned,
    )


def test_observed_322_partial_prune_keeps_orphans_prune_only():
    catalog, protections, remote, replacement_ids, already_pruned_ids = _observed_322_fixture()

    plan = reconcile(catalog, protections, remote, replacement_mode=True)

    assert plan.ready
    assert plan.classification == "remote_counts_3/2/2"
    assert [item.backup_id for item in plan.replacement_candidates] == replacement_ids
    assert [item.backup_id for item in plan.pruned_protection_corrections] == already_pruned_ids
    assert [item.kind for item in plan.prune_candidates] == ["orphan", "orphan", "orphan"]
    assert {item.backup_id for item in plan.prune_candidates}.isdisjoint(
        {item.backup_id for item in plan.replacement_candidates}
    )
    assert plan.imports == ()


def test_remote_orphan_bound_and_recency_are_fail_closed():
    catalog, protections, remote, _replacement_ids, _already_pruned_ids = _observed_322_fixture()
    profile_id = "minecraft-sunlit-cobblemon"

    too_many = reconcile(
        catalog,
        protections,
        (*remote, _orphan(profile_id, 999)),
        replacement_mode=True,
    )
    assert not too_many.ready
    assert "remote orphan count exceeds bound" in too_many.refusals

    newer = _orphan(profile_id, 998)
    newer_id = "20270805T000000000000Z-000000000998"
    newer = RemoteGeneration(
        **{
            **newer.__dict__,
            "backup_id": newer_id,
            "remote_key": f"{b2_prefix(profile_id)}/{newer_id}.tar.zst",
            "manifest_backup_id": newer_id,
        }
    )
    without_old_orphan = tuple(
        item for item in remote if not (item.profile_id == profile_id and item.manifest_verified)
    )
    ambiguous = reconcile(
        catalog,
        protections,
        (*without_old_orphan, newer),
        replacement_mode=True,
    )
    assert not ambiguous.ready
    assert "remote orphan recency is ambiguous" in ambiguous.refusals


@pytest.mark.parametrize(
    "change",
    ("unverified", "wrong_prefix", "wrong_manifest", "wrong_size", "missing_replacement"),
)
def test_observed_partial_prune_refuses_unproven_inputs(change):
    catalog, protections, remote, _replacement_ids, _already_pruned_ids = _observed_322_fixture()
    catalog = list(catalog)
    remote = list(remote)
    orphan_index = next(index for index, item in enumerate(remote) if item.manifest_verified)
    orphan = remote[orphan_index]
    if change == "unverified":
        remote[orphan_index] = RemoteGeneration(**{**orphan.__dict__, "manifest_verified": False})
    elif change == "wrong_prefix":
        remote[orphan_index] = RemoteGeneration(**{**orphan.__dict__, "remote_key": "wrong/prefix.tar.zst"})
    elif change == "wrong_manifest":
        remote[orphan_index] = RemoteGeneration(**{**orphan.__dict__, "manifest_backup_id": _backup_id(999)})
    elif change == "wrong_size":
        remote[orphan_index] = RemoteGeneration(**{**orphan.__dict__, "downloaded_size_bytes": orphan.size_bytes + 1})
    else:
        target_index = next(
            index
            for index, item in enumerate(catalog)
            if item.profile_id == "terraria-vanilla" and item.backup_id not in {r.backup_id for r in remote}
        )
        catalog[target_index] = CatalogGeneration(
            **{**catalog[target_index].__dict__, "archive_size_bytes": -1}
        )

    plan = reconcile(tuple(catalog), protections, tuple(remote), replacement_mode=True)

    assert not plan.ready
    assert plan.prune_candidates == ()
    assert plan.replacement_candidates == ()


def test_observed_322_apply_orders_replacements_then_orphan_prunes_and_redryrun_is_clean(
    tmp_path, monkeypatch
):
    catalog, protections, remote, replacement_ids, already_pruned_ids = _observed_322_fixture()
    replacement_set = set(replacement_ids)
    metadata = {}
    with_paths = []
    for item in catalog:
        path = None
        if item.backup_id in replacement_set:
            path = tmp_path / f"{item.backup_id}.tar.zst"
            path.write_bytes(b"replacement")
            metadata[path] = (item.size_bytes, item.sha256)
        with_paths.append(CatalogGeneration(**{**item.__dict__, "archive_path": path}))
    catalog = tuple(with_paths)
    monkeypatch.setattr(
        "game_control.backup_reconcile._local_archive_metadata",
        lambda path: metadata[path],
    )
    monkeypatch.setattr("game_control.backup_reconcile._validate_archive_manifest", lambda *_args: None)
    plan = reconcile(catalog, protections, remote, replacement_mode=True)
    connection = _database(catalog, protections)

    class _Transport:
        def __init__(self):
            self.objects = {item.remote_key: item.size_bytes for item in remote}
            self.calls = []

        def list(self, prefix):
            return tuple(
                RemoteObject(key, size)
                for key, size in self.objects.items()
                if key.startswith(prefix + "/")
            )

        def upload(self, source, key):
            self.calls.append(("upload", key))
            self.objects[key] = metadata[source][0]

        def verify(self, source, key):
            self.calls.append(("verify", key))
            assert self.objects[key] == metadata[source][0]

        def delete(self, key):
            self.calls.append(("delete", key))
            self.objects.pop(key)

    transport = _Transport()
    apply_replacement_plan(connection, transport, plan)

    first_delete = next(index for index, call in enumerate(transport.calls) if call[0] == "delete")
    assert all(call[0] in {"upload", "verify"} for call in transport.calls[:first_delete])
    assert [call[0] for call in transport.calls].count("upload") == 2
    assert [call[0] for call in transport.calls].count("verify") == 2
    assert [call[0] for call in transport.calls].count("delete") == 3
    assert connection.execute(
        "SELECT prune_state FROM backup_protections WHERE backup_id=?",
        (already_pruned_ids[0],),
    ).fetchone()[0] == "deleted"
    assert connection.execute(
        "SELECT protected FROM backups WHERE id=?", (already_pruned_ids[0],)
    ).fetchone()[0] == 0

    protected_by_id = dict(connection.execute("SELECT id,protected FROM backups"))
    second_catalog = tuple(
        CatalogGeneration(**{**item.__dict__, "protected": bool(protected_by_id[item.backup_id])})
        for item in catalog
    )
    second_protections = tuple(
        ProtectionGeneration(
            backup_id=row[0],
            profile_id=row[1],
            remote_key=row[2],
            local_sha256=row[3],
            local_verified=bool(row[4]),
            upload_state=row[5],
            remote_verified=bool(row[6]),
            comparison_state=row[7],
            prune_state=row[8],
            error_code=row[9],
        )
        for row in connection.execute(
            "SELECT backup_id,profile_id,remote_key,local_sha256,local_verified,upload_state,"
            "remote_verified,comparison_state,prune_state,error_code FROM backup_protections"
        )
    )
    catalog_by_id = {item.backup_id: item for item in second_catalog}
    second_remote = tuple(
        RemoteGeneration(
            profile_id=key.split("/")[-2],
            backup_id=key.rsplit("/", 1)[-1].removesuffix(".tar.zst"),
            size_bytes=size,
            sha256=catalog_by_id[key.rsplit("/", 1)[-1].removesuffix(".tar.zst")].sha256,
            remote_key=key,
        )
        for key, size in transport.objects.items()
    )
    second = reconcile(second_catalog, second_protections, second_remote, replacement_mode=True)
    assert second.ready
    assert second.imports == ()
    assert second.replacement_candidates == ()
    assert second.prune_candidates == ()
    assert second.protected_flag_corrections == ()
    assert second.pruned_protection_corrections == ()


def test_replacement_apply_refuses_remote_inventory_race_before_any_action():
    catalog, protections, remote, _replacement_ids, _already_pruned_ids = _observed_322_fixture()
    plan = reconcile(catalog, protections, remote, replacement_mode=True)
    connection = _database(catalog, protections)

    class _ChangedTransport:
        def __init__(self):
            self.calls = []

        def list(self, prefix):
            rows = [item for item in remote if item.remote_key.startswith(prefix + "/")]
            return tuple(RemoteObject(item.remote_key, item.size_bytes + 1) for item in rows)

        def upload(self, *_args):
            self.calls.append("upload")

        def verify(self, *_args):
            self.calls.append("verify")

        def delete(self, *_args):
            self.calls.append("delete")

    transport = _ChangedTransport()
    with pytest.raises(SafeError, match="remote inventory changed before replacement"):
        apply_replacement_plan(connection, transport, plan)
    assert transport.calls == []


def test_replacement_apply_refuses_already_pruned_protection_digest_race():
    catalog, protections, remote, _replacement_ids, already_pruned_ids = _observed_322_fixture()
    plan = reconcile(catalog, protections, remote, replacement_mode=True)
    connection = _database(catalog, protections)
    connection.execute(
        "UPDATE backup_protections SET local_sha256=? WHERE backup_id=?",
        ("0" * 64, already_pruned_ids[0]),
    )
    connection.commit()

    class _Transport:
        def list(self, _prefix):
            raise AssertionError("remote inventory must not be read after catalog drift")

    with pytest.raises(SafeError, match="already-pruned protection changed"):
        apply_replacement_plan(connection, _Transport(), plan)


def test_replacement_apply_refuses_missing_plan_bound_prune_key():
    orphan = _orphan("minecraft-sunlit-cobblemon", 1)
    candidate = PruneCandidate(
        profile_id=orphan.profile_id,
        backup_id=orphan.backup_id,
        size_bytes=orphan.size_bytes,
        sha256=orphan.sha256,
        kind="orphan",
        remote_key=orphan.remote_key,
    )
    plan = ReconciliationPlan(
        classification="synthetic_missing_prune_key",
        profile_summary=(),
        imports=(),
        protected_flag_corrections=(),
        prune_candidates=(candidate,),
    )

    class _Transport:
        def __init__(self):
            self.deleted = []

        def list(self, _prefix):
            return ()

        def delete(self, key):
            self.deleted.append(key)

    transport = _Transport()
    with pytest.raises(SafeError, match="prune candidate disappeared"):
        apply_replacement_plan(_database((), ()), transport, plan)
    assert transport.deleted == []


def test_replacement_apply_refuses_duplicate_prune_key_before_remote_access():
    catalog, protections, remote, _replacement_ids, _already_pruned_ids = _observed_322_fixture()
    clean = reconcile(catalog, protections, remote, replacement_mode=True)
    duplicate = replace(clean, prune_candidates=(clean.prune_candidates[0],) * 2)

    class _Transport:
        def list(self, _prefix):
            raise AssertionError("duplicate plan must fail before remote access")

    with pytest.raises(SafeError, match="prune plan is not unique"):
        apply_replacement_plan(_database(catalog, protections), _Transport(), duplicate)


def test_ordinary_prune_zero_row_rolls_back_all_catalog_transitions():
    catalog, protections, remote = _fixture()
    plan = reconcile(catalog, protections, remote, replacement_mode=True)
    ordinary = next(item for item in plan.prune_candidates if item.kind == "older")
    connection = _database(catalog, protections)
    before_backups = connection.execute("SELECT * FROM backups ORDER BY id").fetchall()
    before_protections = connection.execute(
        "SELECT * FROM backup_protections ORDER BY backup_id"
    ).fetchall()
    connection.executescript(
        f"""
        CREATE TRIGGER suppress_prune_update
        BEFORE UPDATE OF prune_state ON backup_protections
        WHEN OLD.backup_id = '{ordinary.backup_id}'
        BEGIN
            SELECT RAISE(IGNORE);
        END;
        """
    )

    class _Transport:
        def __init__(self):
            self.objects = {item.remote_key: item.size_bytes for item in remote}

        def list(self, prefix):
            return tuple(
                RemoteObject(key, size)
                for key, size in self.objects.items()
                if key.startswith(prefix + "/")
            )

        def delete(self, key):
            self.objects.pop(key)

    with pytest.raises(SafeError, match="prune protection changed before catalog commit"):
        apply_replacement_plan(connection, _Transport(), plan)
    assert connection.execute("SELECT * FROM backups ORDER BY id").fetchall() == before_backups
    assert connection.execute(
        "SELECT * FROM backup_protections ORDER BY backup_id"
    ).fetchall() == before_protections


def _fixture():
    catalog = []
    remote = []
    protections = []
    for profile_offset, profile_id in enumerate(RETAINED_PROFILE_IDS):
        count = 2 if profile_id == RETAINED_PROFILE_IDS[0] else 3
        rows = [_catalog(profile_id, profile_offset * 10 + index) for index in range(count)]
        catalog.extend(rows)
        remote.extend(_remote(item) for item in rows)
        for item in rows[: count - 1]:
            protections.append(
                ProtectionGeneration(
                    backup_id=item.backup_id,
                    profile_id=profile_id,
                    remote_key=f"{b2_prefix(profile_id)}/{item.backup_id}.tar.zst",
                    local_sha256=item.sha256,
                    local_verified=True,
                    remote_verified=True,
                    comparison_state="verified",
                )
            )
    return tuple(catalog), tuple(protections), tuple(remote)


def _database(catalog, protections) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE backups (
            id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, created_at TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, verified INTEGER NOT NULL, protected INTEGER NOT NULL
        );
        CREATE TABLE backup_protections (
            backup_id TEXT NOT NULL, profile_id TEXT NOT NULL, destination_id TEXT NOT NULL,
            backup_class TEXT NOT NULL, remote_key TEXT NOT NULL, local_sha256 TEXT NOT NULL,
            local_verified INTEGER NOT NULL, upload_state TEXT NOT NULL,
            remote_verified INTEGER NOT NULL, comparison_state TEXT NOT NULL,
            prune_state TEXT NOT NULL, updated_at TEXT NOT NULL, error_code TEXT,
            PRIMARY KEY (backup_id, destination_id, backup_class), UNIQUE(remote_key),
            FOREIGN KEY (backup_id) REFERENCES backups(id)
        );
        """
    )
    for item in catalog:
        connection.execute(
            "INSERT INTO backups VALUES (?,?,?,?,?,?)",
            (item.backup_id, item.profile_id, "2026-08-05T00:00:00+00:00", item.size_bytes, 1, int(item.protected)),
        )
    for item in protections:
        connection.execute(
            "INSERT INTO backup_protections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item.backup_id,
                item.profile_id,
                "horizon-b2",
                "application",
                item.remote_key,
                item.local_sha256,
                int(item.local_verified),
                "succeeded",
                int(item.remote_verified),
                item.comparison_state,
                item.prune_state,
                "2026-08-05T00:00:00+00:00",
                None,
            ),
        )
    connection.commit()
    return connection


def _captured(connection: sqlite3.Connection):
    catalog = [
        CatalogGeneration(
            backup_id=row[0],
            profile_id=row[1],
            size_bytes=row[2],
            archive_size_bytes=row[2],
            sha256=hashlib.sha256(f"{row[1]}:{row[0]}".encode()).hexdigest(),
            verified=bool(row[3]),
            protected=bool(row[4]),
        )
        for row in connection.execute("SELECT id,profile_id,size_bytes,verified,protected FROM backups")
    ]
    protections = [
        ProtectionGeneration(
            backup_id=row[0],
            profile_id=row[1],
            remote_key=row[2],
            local_sha256=row[3],
            local_verified=bool(row[4]),
            remote_verified=bool(row[5]),
            comparison_state=row[6],
        )
        for row in connection.execute(
            "SELECT backup_id,profile_id,remote_key,local_sha256,local_verified,remote_verified,comparison_state "
            "FROM backup_protections"
        )
    ]
    return catalog, protections


def test_current_233_is_classified_and_prune_candidates_are_exact():
    catalog, protections, remote = _fixture()

    plan = reconcile(catalog, protections, remote)

    assert plan.ready
    assert plan.classification == "remote_counts_2/3/3"
    assert len(plan.imports) == 3
    assert [(item.profile_id, item.backup_id) for item in plan.prune_candidates] == [
        ("terraria-vanilla", _backup_id(10)),
        ("terraria-tmod", _backup_id(20)),
    ]
    public = plan.to_public_dict()
    assert all("remote_key" not in item for item in public["legacy_protection_imports"])


def test_apply_is_separate_and_idempotent():
    catalog, protections, remote = _fixture()
    connection = _database(catalog, protections)
    plan = reconcile(catalog, protections, remote)

    before = connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0]
    assert before == 5
    apply_plan(connection, plan, now=datetime(2026, 8, 6, tzinfo=timezone.utc))
    assert connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 8
    assert connection.execute("SELECT COUNT(*) FROM backups WHERE protected=0").fetchone()[0] == 0

    new_catalog, new_protections = _captured(connection)
    second = reconcile(new_catalog, new_protections, remote)
    assert second.ready
    assert second.imports == ()
    apply_plan(connection, second)
    assert connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 8
    assert connection.execute("SELECT COUNT(*) FROM backups WHERE protected=0").fetchone()[0] == 0


def test_mismatch_refusal_has_no_actions():
    catalog, protections, remote = _fixture()
    bad = list(remote)
    bad[0] = _remote(catalog[0], size=catalog[0].size_bytes + 1)

    plan = reconcile(catalog, protections, bad)

    assert not plan.ready
    assert "remote archive does not match verified catalog" in plan.refusals
    assert plan.imports == ()
    assert plan.prune_candidates == ()


def test_replacement_mode_requires_manifest_proof_and_orders_profile_actions():
    catalog, protections, remote = _fixture()
    missing = {profile_id: max(item.backup_id for item in catalog if item.profile_id == profile_id) for profile_id in RETAINED_PROFILE_IDS}
    replacement_remote = []
    replacement_protections = [item for item in protections if item.backup_id not in missing.values()]
    for item in remote:
        if item.backup_id == missing[item.profile_id]:
            replacement_remote.append(_orphan(item.profile_id, 100 + RETAINED_PROFILE_IDS.index(item.profile_id)))
        else:
            replacement_remote.append(item)

    plan = reconcile(catalog, replacement_protections, replacement_remote, replacement_mode=True)

    assert plan.ready
    assert len(plan.remote_orphans) == 3
    assert [item.profile_id for item in plan.replacement_candidates] == list(RETAINED_PROFILE_IDS)
    assert [(item.profile_id, item.backup_id, item.target_protected) for item in plan.protected_flag_corrections] == [
        ("terraria-vanilla", _backup_id(10), False),
        ("terraria-tmod", _backup_id(20), False),
    ]
    assert {item.kind for item in plan.prune_candidates} == {"older", "orphan"}
    assert sum(item.kind == "orphan" for item in plan.prune_candidates) == 3
    methods = {item["method"] for item in plan.to_public_dict()["remote_verification"]}
    assert "manifest" in methods


def test_replacement_mode_refuses_manifest_identity_mismatch():
    catalog, protections, _remote_rows = _fixture()
    bad = _orphan("minecraft-sunlit-cobblemon", 999)
    bad = RemoteGeneration(**{**bad.__dict__, "manifest_profile_id": "terraria-tmod"})

    plan = reconcile(catalog, protections, (bad,), replacement_mode=True)

    assert not plan.ready
    assert "unknown remote generation" in plan.refusals


def test_bounded_temp_staging_downloads_and_verifies_manifest(tmp_path):
    profile_id = "minecraft-sunlit-cobblemon"
    backup_id = "20260806T000000000000Z-0123456789ab"
    archive = tmp_path / f"{backup_id}.tar.zst"
    manifest = {
        "schema": 1,
        "profile_id": profile_id,
        "backup_id": backup_id,
    }
    payload = json.dumps(manifest).encode()
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))

    class _Transport:
        def download(self, _key, destination):
            destination.write_bytes(archive.read_bytes())

    remote = RemoteObject(
        key=f"{b2_prefix(profile_id)}/{backup_id}.tar.zst",
        size_bytes=archive.stat().st_size,
    )
    result = _download_and_verify_manifest(_Transport(), remote, profile_id, backup_id)
    assert result == (profile_id, backup_id, hashlib.sha256(archive.read_bytes()).hexdigest(), remote.size_bytes)


def test_build_fixed_plan_stages_each_remote_only_generation(tmp_path, monkeypatch):
    roots = {profile_id: tmp_path / profile_id for profile_id in RETAINED_PROFILE_IDS}
    stage_root = tmp_path / "staging"
    stage_root.mkdir()
    catalog = []
    remote_rows = []
    for index, profile_id in enumerate(RETAINED_PROFILE_IDS):
        roots[profile_id].mkdir()
        local_id = f"20260806T000000000000Z-{index + 1:012x}"
        local_path = roots[profile_id] / f"{local_id}.tar.zst"
        local_manifest = json.dumps({"schema": 1, "profile_id": profile_id, "backup_id": local_id}).encode()
        with tarfile.open(local_path, "w") as output:
            member = tarfile.TarInfo("manifest.json")
            member.size = len(local_manifest)
            output.addfile(member, io.BytesIO(local_manifest))
        catalog.append(
            CatalogGeneration(
                backup_id=local_id,
                profile_id=profile_id,
                size_bytes=local_path.stat().st_size,
                archive_size_bytes=local_path.stat().st_size,
                sha256=hashlib.sha256(local_path.read_bytes()).hexdigest(),
                verified=True,
                protected=True,
            )
        )

        remote_id = f"20240804T000000000000Z-{index + 10:012x}"
        remote_path = tmp_path / f"{remote_id}-{index}.tar.zst"
        remote_manifest = json.dumps({"schema": 1, "profile_id": profile_id, "backup_id": remote_id}).encode()
        with tarfile.open(remote_path, "w") as output:
            member = tarfile.TarInfo("manifest.json")
            member.size = len(remote_manifest)
            output.addfile(member, io.BytesIO(remote_manifest))
        remote_key = f"{b2_prefix(profile_id)}/{remote_id}.tar.zst"
        (stage_root / f"{profile_id}-{remote_id}.tar.zst").write_bytes(remote_path.read_bytes())
        remote_rows.append(RemoteObject(remote_key, remote_path.stat().st_size))

    monkeypatch.setattr("game_control.backup_reconcile.FIXED_BACKUP_ROOTS", roots)
    monkeypatch.setattr("game_control.backup_reconcile.FIXED_REMOTE_STAGING_ROOT", stage_root)

    class _Transport:
        def __init__(self):
            self.verifications = []

        def list(self, prefix):
            return tuple(item for item in remote_rows if item.key.startswith(prefix + "/"))

        def download(self, _key, _destination):
            raise AssertionError("fixed staged evidence should be used")

        def verify(self, source, key):
            assert source.name == key.rsplit("/", 1)[-1]
            assert source.is_file()
            assert source.stat().st_ino == (
                stage_root / f"{key.split('/')[-2]}-{source.name}"
            ).stat().st_ino
            self.verifications.append((source, key))

    transport = _Transport()
    plan = build_fixed_plan(_database(tuple(catalog), ()), transport)
    assert plan.ready
    assert len(plan.remote_orphans) == 3
    assert len(plan.replacement_candidates) == 3
    assert {item["method"] for item in plan.to_public_dict()["remote_verification"]} == {"manifest+cryptcheck"}
    assert len(transport.verifications) == 3


def test_live_generation_sets_upload_exact_replacements_and_prune_newest_two(tmp_path, monkeypatch):
    specs = {
        "minecraft-sunlit-cobblemon": (
            ("20260805T231827577985Z-f0d50fda4ab7", 3259444251, True),
            ("20260805T232400550608Z-c28f40b6f1cf", 3259444254, True),
        ),
        "terraria-vanilla": (
            ("20260805T225801782612Z-a3f80894ce27", 10654116, True),
            ("20260805T232248777985Z-4862a9884563", 10654155, True),
            ("20260806T032002316192Z-06b03e752b9f", 10654052, False),
        ),
        "terraria-tmod": (
            ("20260805T225801862092Z-a33c23d38987", 53613516, True),
            ("20260805T232259979227Z-312ac2a9e197", 48822678, True),
            ("20260806T034000659155Z-95b7e8e36a56", 53617940, False),
        ),
    }
    orphan_specs = {
        "minecraft-sunlit-cobblemon": (
            "20260805T192128062227Z-d6386ad13b00", 3402529650,
            "d8dce6c85f6fef9190886ab9dcfac884a564ee15115538791fa6d9de703bade7",
        ),
        "terraria-vanilla": (
            "20260805T192339550411Z-408122f49756", 673680047,
            "b22ce524d927518acde190702760b43f28332e2dc57665859a72dd55a3a8f9c9",
        ),
        "terraria-tmod": (
            "20260805T192417939266Z-c9049e3bef0d", 238399968,
            "873ff9a289f9be32dc7be8c48b4da3d28db0692deeb987f403b3ea514fef6deb",
        ),
    }
    catalog = []
    remote = []
    protections = []
    replacement_metadata = {}
    for profile_id in RETAINED_PROFILE_IDS:
        rows = []
        for backup_id, size_bytes, protected in specs[profile_id]:
            digest = hashlib.sha256(f"live-catalog:{profile_id}:{backup_id}".encode()).hexdigest()
            archive_path = None
            if backup_id == specs[profile_id][1][0]:
                archive_path = tmp_path / f"{backup_id}.tar.zst"
                archive_path.write_bytes(b"replacement-placeholder")
                replacement_metadata[archive_path] = (size_bytes, digest)
            item = CatalogGeneration(
                backup_id=backup_id,
                profile_id=profile_id,
                size_bytes=size_bytes,
                archive_size_bytes=size_bytes,
                sha256=digest,
                verified=True,
                protected=protected,
                archive_path=archive_path,
            )
            rows.append(item)
            catalog.append(item)
        protected_remote_ids = {rows[0].backup_id}
        if profile_id != "minecraft-sunlit-cobblemon":
            protected_remote_ids.add(rows[2].backup_id)
        for item in rows:
            if item.backup_id in protected_remote_ids:
                protections.append(
                    ProtectionGeneration(
                        backup_id=item.backup_id,
                        profile_id=profile_id,
                        remote_key=f"{b2_prefix(profile_id)}/{item.backup_id}.tar.zst",
                        local_sha256=item.sha256,
                        local_verified=True,
                        remote_verified=True,
                        comparison_state="verified",
                    )
                )
            if item.backup_id != specs[profile_id][1][0]:
                remote.append(
                    RemoteGeneration(
                        profile_id=profile_id,
                        backup_id=item.backup_id,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                        remote_key=f"{b2_prefix(profile_id)}/{item.backup_id}.tar.zst",
                    )
                )
        orphan_id, orphan_size, orphan_sha = orphan_specs[profile_id]
        remote.append(
            RemoteGeneration(
                profile_id=profile_id,
                backup_id=orphan_id,
                size_bytes=orphan_size,
                sha256=orphan_sha,
                remote_key=f"{b2_prefix(profile_id)}/{orphan_id}.tar.zst",
                manifest_profile_id=profile_id,
                manifest_backup_id=orphan_id,
                manifest_verified=True,
                downloaded_size_bytes=orphan_size,
                verification_method="manifest",
            )
        )

    plan = reconcile(catalog, protections, remote, replacement_mode=True)

    assert plan.ready
    assert [item.backup_id for item in plan.replacement_candidates] == [
        "20260805T232400550608Z-c28f40b6f1cf",
        "20260805T232248777985Z-4862a9884563",
        "20260805T232259979227Z-312ac2a9e197",
    ]
    assert [(item.profile_id, item.backup_id, item.kind) for item in plan.prune_candidates] == [
        ("minecraft-sunlit-cobblemon", "20260805T192128062227Z-d6386ad13b00", "orphan"),
        ("terraria-vanilla", "20260805T192339550411Z-408122f49756", "orphan"),
        ("terraria-vanilla", "20260805T225801782612Z-a3f80894ce27", "older"),
        ("terraria-tmod", "20260805T192417939266Z-c9049e3bef0d", "orphan"),
        ("terraria-tmod", "20260805T225801862092Z-a33c23d38987", "older"),
    ]
    assert [
        (item.profile_id, item.backup_id, item.target_protected)
        for item in plan.protected_flag_corrections
    ] == [
        ("terraria-vanilla", "20260805T225801782612Z-a3f80894ce27", False),
        ("terraria-vanilla", "20260806T032002316192Z-06b03e752b9f", True),
        ("terraria-tmod", "20260805T225801862092Z-a33c23d38987", False),
        ("terraria-tmod", "20260806T034000659155Z-95b7e8e36a56", True),
    ]
    assert all(len(item.sha256) == 64 for item in plan.remote_orphans)

    monkeypatch.setattr(
        "game_control.backup_reconcile._local_archive_metadata",
        lambda path: replacement_metadata[path],
    )
    monkeypatch.setattr(
        "game_control.backup_reconcile._validate_archive_manifest",
        lambda *_args: None,
    )

    class _Transport:
        def __init__(self):
            self.objects = {item.remote_key: item.size_bytes for item in remote}
            self.calls = []

        def list(self, prefix):
            return tuple(
                RemoteObject(key, size)
                for key, size in self.objects.items()
                if key.startswith(prefix + "/")
            )

        def upload(self, source, key):
            self.calls.append(("upload", key, source))
            self.objects[key] = replacement_metadata[source][0]

        def verify(self, source, key):
            self.calls.append(("verify", key, source))
            assert self.objects[key] == replacement_metadata[source][0]

        def delete(self, key):
            self.calls.append(("delete", key, None))
            self.objects.pop(key, None)

    transport = _Transport()
    with pytest.raises(SafeError, match="exact replacement apply"):
        apply_plan(_database(catalog, protections), plan)
    connection = _database(catalog, protections)
    apply_replacement_plan(connection, transport, plan)

    expected_replacement_keys = [
        f"{b2_prefix(profile_id)}/{backup_id}.tar.zst"
        for profile_id, backup_id in (
            ("minecraft-sunlit-cobblemon", "20260805T232400550608Z-c28f40b6f1cf"),
            ("terraria-vanilla", "20260805T232248777985Z-4862a9884563"),
            ("terraria-tmod", "20260805T232259979227Z-312ac2a9e197"),
        )
    ]
    assert [key for action, key, _source in transport.calls if action == "upload"] == expected_replacement_keys
    assert [key for action, key, _source in transport.calls if action == "verify"] == expected_replacement_keys
    assert [key for action, key, _source in transport.calls if action == "delete"] == [
        f"{b2_prefix('minecraft-sunlit-cobblemon')}/20260805T192128062227Z-d6386ad13b00.tar.zst",
        f"{b2_prefix('terraria-vanilla')}/20260805T192339550411Z-408122f49756.tar.zst",
        f"{b2_prefix('terraria-vanilla')}/20260805T225801782612Z-a3f80894ce27.tar.zst",
        f"{b2_prefix('terraria-tmod')}/20260805T192417939266Z-c9049e3bef0d.tar.zst",
        f"{b2_prefix('terraria-tmod')}/20260805T225801862092Z-a33c23d38987.tar.zst",
    ]
    assert connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 8
    assert connection.execute(
        "SELECT COUNT(*) FROM backup_protections WHERE prune_state='deleted'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT protected FROM backups WHERE id='20260806T032002316192Z-06b03e752b9f'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT protected FROM backups WHERE id='20260806T034000659155Z-95b7e8e36a56'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT protected FROM backups WHERE id='20260805T225801782612Z-a3f80894ce27'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT protected FROM backups WHERE id='20260805T225801862092Z-a33c23d38987'"
    ).fetchone()[0] == 0


def test_protection_state_mismatch_refuses_full_row_state():
    catalog, _protections, remote = _fixture()
    item = catalog[0]
    bad = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=f"{b2_prefix(item.profile_id)}/{item.backup_id}.tar.zst",
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
        prune_state="succeeded",
        upload_state="failed",
        error_code="upload_failed",
    )

    plan = reconcile(catalog, (bad,), remote)

    assert not plan.ready
    assert "protection verification state is not proven" in plan.refusals


def test_unprotected_retained_generation_is_promoted_idempotently():
    item = _catalog("terraria-tmod", 1, protected=False)
    remote = _remote(item)
    protection = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=remote.remote_key,
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
    )
    plan = reconcile((item,), (protection,), (remote,), replacement_mode=True)

    assert plan.ready
    assert plan.protected_flag_corrections == (
        ProtectedFlagCorrection(item.profile_id, item.backup_id, True),
    )
    connection = _database((item,), (protection,))
    apply_plan(connection, plan)
    assert connection.execute("SELECT protected FROM backups").fetchone()[0] == 1

    second_catalog, second_protections = _captured(connection)
    second = reconcile(second_catalog, second_protections, (remote,), replacement_mode=True)
    assert second.ready
    assert second.protected_flag_corrections == ()
    apply_plan(connection, second)
    assert connection.execute("SELECT protected FROM backups").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (("remote_verified", False), ("comparison_state", "mismatch"), ("remote_key", "not/canonical")),
)
def test_unprotected_retained_generation_refuses_incomplete_cryptcheck_proof(field, value):
    item = _catalog("terraria-tmod", 1, protected=False)
    remote = _remote(item)
    clean = ProtectionGeneration(
        backup_id=item.backup_id,
        profile_id=item.profile_id,
        remote_key=remote.remote_key,
        local_sha256=item.sha256,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
    )
    bad = ProtectionGeneration(**{**clean.__dict__, field: value})
    plan = reconcile((item,), (bad,), (remote,), replacement_mode=True)

    assert not plan.ready
    assert plan.protected_flag_corrections == ()
    assert any(
        refusal in plan.refusals
        for refusal in (
            "protection verification state is not proven",
            "protection destination is not canonical",
            "unprotected retained generation lacks canonical cryptcheck proof",
        )
    )


def test_replacement_apply_verifies_uploads_before_exact_prune_and_updates_state(tmp_path, monkeypatch):
    contents = {index: f"archive-{index}".encode() for index in (1, 2)}
    catalog = []
    for index, content in contents.items():
        path = tmp_path / f"{_backup_id(index)}.tar.zst"
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        catalog.append(
            CatalogGeneration(
                backup_id=_backup_id(index),
                profile_id="minecraft-sunlit-cobblemon",
                size_bytes=len(content),
                archive_size_bytes=len(content),
                sha256=digest,
                verified=True,
                protected=True,
                archive_path=path,
            )
        )
    orphan = _orphan("minecraft-sunlit-cobblemon", 0)

    class _Transport:
        def __init__(self):
            self.objects = {orphan.remote_key: orphan.size_bytes}
            self.calls = []

        def list(self, prefix):
            return tuple(RemoteObject(key, size) for key, size in self.objects.items() if key.startswith(prefix + "/"))

        def upload(self, source, key):
            self.calls.append(("upload", key))
            self.objects[key] = source.stat().st_size

        def verify(self, source, key):
            self.calls.append(("verify", key))
            assert self.objects[key] == source.stat().st_size

        def delete(self, key):
            self.calls.append(("delete", key))
            self.objects.pop(key, None)

    transport = _Transport()
    monkeypatch.setattr(
        "game_control.backup_reconcile._validate_archive_manifest",
        lambda *_args: None,
    )
    plan = reconcile(catalog, (), (orphan,), replacement_mode=True)
    connection = _database(catalog, ())

    assert len(plan.replacement_candidates) == 2
    assert [item.kind for item in plan.prune_candidates] == ["orphan"]
    apply_replacement_plan(connection, transport, plan)

    first_delete = next(index for index, call in enumerate(transport.calls) if call[0] == "delete")
    assert all(call[0] in {"upload", "verify"} for call in transport.calls[:first_delete])
    assert connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 2
    assert orphan.remote_key not in transport.objects


def test_unknown_row_refusal_and_redaction():
    catalog, protections, remote = _fixture()
    unknown = ProtectionGeneration(
        backup_id=_backup_id(999),
        profile_id="minecraft-sunlit-cobblemon",
        remote_key="helios-b2-crypt:token=never-print/unknown.tar.zst",
        local_sha256="0" * 64,
        local_verified=True,
        remote_verified=True,
        comparison_state="verified",
    )

    plan = reconcile(catalog, (*protections, unknown), remote)
    rendered = plan_json(plan)

    assert not plan.ready
    assert "unknown protection row" in plan.refusals
    assert "token=never-print" not in rendered
    assert "unknown.tar.zst" not in rendered


def test_protected_flag_correction_is_an_explicit_idempotent_transition():
    item = _catalog("minecraft-sunlit-cobblemon", 1, protected=True)
    plan = reconcile((item,), (), ())
    connection = _database((item,), ())

    assert plan.ready
    assert len(plan.protected_flag_corrections) == 1
    assert plan.protected_flag_corrections[0].backup_id == item.backup_id
    apply_plan(connection, plan)
    assert connection.execute("SELECT protected FROM backups").fetchone()[0] == 0
    second_catalog, second_protections = _captured(connection)
    second = reconcile(second_catalog, second_protections, ())
    assert second.protected_flag_corrections == ()


def test_apply_refuses_a_plan_with_mismatch():
    catalog, protections, remote = _fixture()
    bad = list(remote)
    bad[0] = _remote(catalog[0], size=0)
    connection = _database(catalog, protections)

    with pytest.raises(SafeError, match="reconciliation was refused"):
        apply_plan(connection, reconcile(catalog, protections, bad))
    assert connection.execute("SELECT COUNT(*) FROM backup_protections").fetchone()[0] == 5
