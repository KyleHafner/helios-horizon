"""Synthetic public regressions for G11 catalog reconciliation."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

import pytest


def _generation(counter: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp[:-1]}{counter:01d}Z-{uuid4().hex[:12]}"


def _catalog(module, profile_id: str, backup_id: str, *, protected: bool = True):
    payload = f"synthetic:{profile_id}:{backup_id}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    return module.CatalogGeneration(
        backup_id=backup_id,
        profile_id=profile_id,
        size_bytes=len(payload),
        archive_size_bytes=len(payload),
        sha256=digest,
        verified=True,
        protected=protected,
    )


def _protection(module, catalog, *, prune_state="not_started", **changes):
    values = {
        "backup_id": catalog.backup_id,
        "profile_id": catalog.profile_id,
        "remote_key": f"{module.b2_prefix(catalog.profile_id)}/{catalog.backup_id}.tar.zst",
        "local_sha256": catalog.sha256,
        "local_verified": True,
        "remote_verified": True,
        "comparison_state": "verified",
        "prune_state": prune_state,
    }
    values.update(changes)
    return module.ProtectionGeneration(**values)


def _remote(module, catalog):
    return module.RemoteGeneration(
        profile_id=catalog.profile_id,
        backup_id=catalog.backup_id,
        size_bytes=catalog.size_bytes,
        sha256=catalog.sha256,
        remote_key=f"{module.b2_prefix(catalog.profile_id)}/{catalog.backup_id}.tar.zst",
    )


def test_deleted_protection_correction_is_catalog_only_and_idempotent():
    import game_control.backup_reconcile as module

    profile_id = module.RETAINED_PROFILE_IDS[0]
    older = _catalog(module, profile_id, _generation(1), protected=True)
    retained_one = _catalog(module, profile_id, _generation(2), protected=True)
    retained_two = _catalog(module, profile_id, _generation(3), protected=True)
    deleted = _protection(module, older, prune_state="deleted")
    active = (_protection(module, retained_one), _protection(module, retained_two))
    remote = (_remote(module, retained_one), _remote(module, retained_two))

    plan = module.reconcile((older, retained_one, retained_two), (deleted, *active), remote, replacement_mode=True)
    assert plan.ready
    assert plan.prune_candidates == ()
    assert plan.protected_flag_corrections == (
        module.ProtectedFlagCorrection(profile_id, older.backup_id, False),
    )

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE backups (id TEXT, profile_id TEXT, protected INTEGER, verified INTEGER)")
    connection.execute("INSERT INTO backups VALUES (?,?,?,?)", (older.backup_id, profile_id, 1, 1))
    sentinel = bytearray(b"synthetic-local-artifact")
    module.apply_plan(connection, plan)
    assert bytes(sentinel) == b"synthetic-local-artifact"
    assert connection.execute("SELECT protected FROM backups").fetchone()[0] == 0
    second = module.reconcile(
        (older.__class__(**{**older.__dict__, "protected": False}), retained_one, retained_two),
        (deleted, *active),
        remote,
        replacement_mode=True,
    )
    assert second.ready
    assert second.protected_flag_corrections == ()


@pytest.mark.parametrize("bad_field", ["local_sha256", "remote_key", "remote_verified", "comparison_state", "upload_state", "error_code"])
def test_deleted_protection_requires_complete_synthetic_proof(bad_field):
    import game_control.backup_reconcile as module

    catalog = _catalog(module, module.RETAINED_PROFILE_IDS[0], _generation(1))
    clean = _protection(module, catalog, prune_state="deleted")
    values = dict(clean.__dict__)
    values[bad_field] = {
        "local_sha256": "0" * 64,
        "remote_key": "example.com/synthetic.tar.zst",
        "remote_verified": False,
        "comparison_state": "mismatch",
        "upload_state": "failed",
        "error_code": "synthetic-error",
    }[bad_field]
    plan = module.reconcile((catalog,), (module.ProtectionGeneration(**values),), (), replacement_mode=True)
    assert not plan.ready
    assert plan.protected_flag_corrections == ()


def test_deleted_protection_without_catalog_or_with_remote_reappearance_refuses():
    import game_control.backup_reconcile as module

    catalog = _catalog(module, module.RETAINED_PROFILE_IDS[0], _generation(1))
    deleted = _protection(module, catalog, prune_state="deleted")
    missing = module.reconcile((), (deleted,), (), replacement_mode=True)
    assert not missing.ready
    assert "deleted protection row has no catalog generation" in missing.refusals

    reappeared = module.reconcile((catalog,), (deleted,), (_remote(module, catalog),), replacement_mode=True)
    assert not reappeared.ready
    assert "deleted protection has matching remote generation" in reappeared.refusals


def test_duplicate_or_unrelated_deleted_rows_fail_closed():
    import game_control.backup_reconcile as module

    catalog = _catalog(module, module.RETAINED_PROFILE_IDS[0], _generation(1))
    deleted = _protection(module, catalog, prune_state="deleted")
    duplicate = module.reconcile((catalog,), (deleted, deleted), (), replacement_mode=True)
    assert not duplicate.ready
    assert "duplicate deleted protection row" in duplicate.refusals

    unrelated = module.ProtectionGeneration(**{**deleted.__dict__, "profile_id": "example.com"})
    unrelated_plan = module.reconcile((catalog,), (unrelated,), (), replacement_mode=True)
    assert not unrelated_plan.ready
    assert "unknown protection profile" in unrelated_plan.refusals
