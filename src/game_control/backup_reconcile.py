"""Fail-closed reconciliation for the fixed Horizon application backups.

The production entry point has no profile, path, remote, prefix, or retention
arguments.  The pure reconciliation function is intentionally fixture-friendly
so its state transitions can be tested without opening production infrastructure.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .backups import B2CommandTransport, RemoteObject, b2_prefix
from .errors import SafeError


RETAINED_PROFILE_IDS = (
    "minecraft-sunlit-cobblemon",
    "terraria-vanilla",
    "terraria-tmod",
)
RETENTION = 2
DESTINATION_ID = "horizon-b2"
BACKUP_CLASS = "application"
ARCHIVE_SUFFIX = ".tar.zst"
GENERATION_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_STAGED_BYTES = 8 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
FIXED_REMOTE_STAGING_ROOT = Path("/var/tmp/horizon-backup-reconcile")
FIXED_BACKUP_ROOTS = {
    profile_id: Path(f"/var/backups/game-servers/{profile_id}")
    for profile_id in RETAINED_PROFILE_IDS
}


@dataclass(frozen=True)
class CatalogGeneration:
    backup_id: str
    profile_id: str
    size_bytes: int
    sha256: str
    verified: bool
    protected: bool
    archive_size_bytes: int | None = None
    archive_path: Path | None = None


@dataclass(frozen=True)
class ProtectionGeneration:
    backup_id: str
    profile_id: str
    remote_key: str
    local_sha256: str
    local_verified: bool
    remote_verified: bool
    comparison_state: str
    prune_state: str = "not_started"
    upload_state: str = "succeeded"
    error_code: str | None = None


@dataclass(frozen=True)
class RemoteGeneration:
    profile_id: str
    backup_id: str
    size_bytes: int
    sha256: str
    remote_key: str = ""
    manifest_profile_id: str | None = None
    manifest_backup_id: str | None = None
    manifest_verified: bool = False
    downloaded_size_bytes: int | None = None
    verification_method: str = "cryptcheck"


@dataclass(frozen=True)
class ImportProtection:
    profile_id: str
    backup_id: str
    size_bytes: int
    sha256: str
    remote_key: str


@dataclass(frozen=True)
class ProtectedFlagCorrection:
    profile_id: str
    backup_id: str
    target_protected: bool = False


@dataclass(frozen=True)
class PruneCandidate:
    profile_id: str
    backup_id: str
    size_bytes: int
    sha256: str
    kind: str = "older"
    remote_key: str = ""


@dataclass(frozen=True)
class ReplacementCandidate:
    profile_id: str
    backup_id: str
    size_bytes: int
    sha256: str
    archive_path: Path | None = None


@dataclass(frozen=True)
class RemoteOrphan:
    profile_id: str
    backup_id: str
    size_bytes: int
    sha256: str
    remote_key: str


@dataclass(frozen=True)
class ReconciliationPlan:
    classification: str
    profile_summary: tuple[dict[str, int | str], ...]
    imports: tuple[ImportProtection, ...]
    protected_flag_corrections: tuple[ProtectedFlagCorrection, ...]
    prune_candidates: tuple[PruneCandidate, ...]
    refusals: tuple[str, ...] = ()
    remote_evidence: tuple[RemoteGeneration, ...] = ()
    replacement_candidates: tuple[ReplacementCandidate, ...] = ()
    remote_orphans: tuple[RemoteOrphan, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.refusals

    def to_public_dict(self) -> dict[str, object]:
        """Return exact evidence without paths, remote object names, or secrets."""
        return {
            "status": "READY" if self.ready else "HOLD",
            "classification": self.classification,
            "profiles": list(self.profile_summary),
            "legacy_protection_imports": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.imports
            ],
            "protected_flag_corrections": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "target_protected": item.target_protected,
                }
                for item in self.protected_flag_corrections
            ],
            "prune_candidates": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "kind": item.kind,
                }
                for item in self.prune_candidates
            ],
            "remote_verification": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "method": item.verification_method,
                }
                for item in self.remote_evidence
            ],
            "replacement_upload_candidates": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.replacement_candidates
            ],
            "verified_remote_orphans": [
                {
                    "profile_id": item.profile_id,
                    "backup_id": item.backup_id,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.remote_orphans
            ],
            "refusals": list(self.refusals),
        }


def reconcile(
    catalog: Iterable[CatalogGeneration],
    protections: Iterable[ProtectionGeneration],
    remote: Iterable[RemoteGeneration],
    *,
    replacement_mode: bool = False,
) -> ReconciliationPlan:
    """Build a dry-run plan from already captured fixed-scope evidence."""
    catalog_rows = tuple(catalog)
    protection_rows = tuple(protections)
    remote_rows = tuple(remote)
    refusals: set[str] = set()
    deleted_protections: dict[tuple[str, str], ProtectionGeneration] = {}
    for item in protection_rows:
        if item.prune_state != "deleted":
            continue
        key = (item.profile_id, item.backup_id)
        if key in deleted_protections:
            refusals.add("duplicate deleted protection row")
        else:
            deleted_protections[key] = item

    catalogs = _index_catalog(catalog_rows, refusals)
    protections_by_id = _index_protections(protection_rows, catalogs, refusals)
    remotes = _index_remote(remote_rows, catalogs, refusals)
    for profile_id, backup_id in deleted_protections:
        if backup_id not in catalogs.get(profile_id, {}):
            refusals.add("deleted protection row has no catalog generation")
    remote_orphans: list[RemoteOrphan] = []
    missing_protections: list[tuple[str, str, ProtectionGeneration, CatalogGeneration]] = []

    for profile_id, rows in catalogs.items():
        for item in rows.values():
            if item.archive_size_bytes is not None and item.archive_size_bytes != item.size_bytes:
                refusals.add("local archive size differs from catalog")
            if item.size_bytes < 0 or not SHA256_RE.fullmatch(item.sha256):
                refusals.add("local archive metadata is invalid")
            if not item.verified:
                refusals.add("unverified catalog generation is in scope")

    for profile_id, rows in remotes.items():
        for backup_id, item in rows.items():
            expected = catalogs.get(profile_id, {}).get(backup_id)
            if expected is None:
                if replacement_mode and _manifest_proves_orphan(item, profile_id, backup_id):
                    remote_orphans.append(
                        RemoteOrphan(
                            profile_id=profile_id,
                            backup_id=backup_id,
                            size_bytes=item.size_bytes,
                            sha256=item.sha256,
                            remote_key=item.remote_key,
                        )
                    )
                else:
                    refusals.add("unknown remote generation")
                continue
            if (
                item.remote_key != _remote_key(profile_id, backup_id)
                or item.size_bytes < 0
                or item.size_bytes != _archive_size(expected)
                or item.sha256 != expected.sha256
                or not SHA256_RE.fullmatch(item.sha256)
                or item.verification_method not in {"cryptcheck", "manifest", "manifest+cryptcheck"}
                or (
                    item.manifest_verified
                    and not _manifest_proves_orphan(item, profile_id, backup_id)
                )
            ):
                refusals.add("remote archive does not match verified catalog")

    for profile_id, rows in protections_by_id.items():
        for backup_id, item in rows.items():
            expected = catalogs.get(profile_id, {}).get(backup_id)
            if expected is None:
                refusals.add("unknown protection row")
                continue
            if item.remote_key != _remote_key(profile_id, backup_id):
                refusals.add("protection destination is not canonical")
            if item.local_sha256 != expected.sha256:
                refusals.add("protection checksum differs from catalog")
            verification_proven = (
                item.local_verified
                and item.remote_verified
                and item.comparison_state == "verified"
                and item.upload_state == "succeeded"
                and item.prune_state in {"not_started", "succeeded"}
                and item.error_code is None
            )
            if not verification_proven:
                refusals.add("protection verification state is not proven")
            if backup_id not in remotes.get(profile_id, {}):
                if replacement_mode and verification_proven:
                    missing_protections.append((profile_id, backup_id, item, expected))
                else:
                    refusals.add("protection row has no matching remote generation")

    already_pruned: list[PruneCandidate] = []
    for profile_id in RETAINED_PROFILE_IDS:
        missing = [entry for entry in missing_protections if entry[0] == profile_id]
        if not missing:
            continue
        local_rows = catalogs.get(profile_id, {})
        remote_ids = set(remotes.get(profile_id, {}))
        newest_catalog_ids = set(
            sorted(local_rows, key=_generation_sort_key, reverse=True)[:RETENTION]
        )
        if (
            len(remote_ids) != RETENTION
            or remote_ids != newest_catalog_ids
            or any(backup_id in newest_catalog_ids for _profile, backup_id, _row, _catalog in missing)
        ):
            refusals.add("partial prune state is not exact")
            continue
        for _profile, backup_id, row, catalog in missing:
            already_pruned.append(
                PruneCandidate(
                    profile_id=profile_id,
                    backup_id=backup_id,
                    size_bytes=_archive_size(catalog),
                    sha256=catalog.sha256,
                    kind="older",
                    remote_key=row.remote_key,
                )
            )

    classification = "remote_counts_" + "/".join(
        str(len(remotes.get(profile_id, {}))) for profile_id in RETAINED_PROFILE_IDS
    )
    summary = tuple(
        {
            "profile_id": profile_id,
            "catalog_count": len(catalogs.get(profile_id, {})),
            "protection_count": len(protections_by_id.get(profile_id, {})),
            "remote_count": len(remotes.get(profile_id, {})),
        }
        for profile_id in RETAINED_PROFILE_IDS
    )
    remote_evidence = tuple(
        sorted(
            remote_rows,
            key=lambda item: (_profile_order(item.profile_id), _generation_sort_key(item.backup_id)),
        )
    )

    if refusals:
        return ReconciliationPlan(
            classification=classification,
            profile_summary=summary,
            imports=(),
            protected_flag_corrections=(),
            prune_candidates=(),
            refusals=tuple(sorted(refusals)),
            remote_evidence=remote_evidence,
            remote_orphans=tuple(
                sorted(remote_orphans, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
            ),
        )

    imports: list[ImportProtection] = []
    corrections: list[ProtectedFlagCorrection] = []
    candidates: list[PruneCandidate] = list(already_pruned)
    replacements: list[ReplacementCandidate] = []
    already_pruned_ids = {(item.profile_id, item.backup_id) for item in already_pruned}
    for profile_id in RETAINED_PROFILE_IDS:
        local_rows = catalogs.get(profile_id, {})
        remote_profile = remotes.get(profile_id, {})
        protection_profile = protections_by_id.get(profile_id, {})
        for backup_id in sorted(remote_profile, key=_generation_sort_key):
            if backup_id not in protection_profile and backup_id in local_rows:
                if (profile_id, backup_id) in deleted_protections:
                    refusals.add("deleted protection has matching remote generation")
                    continue
                item = local_rows[backup_id]
                imports.append(
                    ImportProtection(
                        profile_id=profile_id,
                        backup_id=backup_id,
                        size_bytes=_archive_size(item),
                        sha256=item.sha256,
                        remote_key=remote_profile[backup_id].remote_key,
                    )
                )
        for backup_id, item in local_rows.items():
            if backup_id not in remote_profile:
                if backup_id in protection_profile:
                    if (profile_id, backup_id) not in already_pruned_ids:
                        refusals.add("protected catalog row has stale protection")
                    elif item.protected:
                        corrections.append(
                            ProtectedFlagCorrection(
                                profile_id=profile_id,
                                backup_id=backup_id,
                                target_protected=False,
                            )
                        )
                elif (profile_id, backup_id) in deleted_protections:
                    deleted = deleted_protections[(profile_id, backup_id)]
                    if not _deleted_protection_proves_prune(deleted, item):
                        refusals.add("deleted protection state is not proven")
                    elif item.protected:
                        corrections.append(
                            ProtectedFlagCorrection(
                                profile_id=profile_id,
                                backup_id=backup_id,
                                target_protected=False,
                            )
                        )
                elif replacement_mode:
                    if not item.protected:
                        refusals.add("unprotected local generation cannot be replacement")
                        continue
                    replacements.append(
                        ReplacementCandidate(
                            profile_id=profile_id,
                            backup_id=backup_id,
                            size_bytes=_archive_size(item),
                            sha256=item.sha256,
                            archive_path=item.archive_path,
                        )
                    )
                elif item.protected:
                    corrections.append(ProtectedFlagCorrection(profile_id, backup_id))
        final_ids = set(remote_profile) | {item.backup_id for item in replacements if item.profile_id == profile_id}
        newest = sorted(final_ids, key=_generation_sort_key, reverse=True)
        retained_ids = set(newest[:RETENTION])
        for backup_id in retained_ids:
            item = local_rows.get(backup_id)
            if item is None or item.protected:
                continue
            protection = protection_profile.get(backup_id)
            remote_item = remote_profile.get(backup_id)
            if not _protection_proves_cryptcheck(protection, item, remote_item):
                refusals.add("unprotected retained generation lacks canonical cryptcheck proof")
                continue
            corrections.append(
                ProtectedFlagCorrection(
                    profile_id=profile_id,
                    backup_id=backup_id,
                    target_protected=True,
                )
            )
        for backup_id in newest[RETENTION:]:
            item = local_rows.get(backup_id)
            remote_item = remote_profile.get(backup_id)
            if item is None and remote_item is None:
                refusals.add("replacement generation set is inconsistent")
                continue
            source = item
            kind = "older"
            remote_key = remote_item.remote_key if remote_item is not None else ""
            if source is None:
                orphan = next(
                    (candidate for candidate in remote_orphans if candidate.profile_id == profile_id and candidate.backup_id == backup_id),
                    None,
                )
                if orphan is None:
                    refusals.add("remote generation has no proven source")
                    continue
                size_bytes, sha256, kind, remote_key = orphan.size_bytes, orphan.sha256, "orphan", orphan.remote_key
            else:
                size_bytes, sha256 = _archive_size(source), source.sha256
            candidates.append(
                PruneCandidate(
                    profile_id=profile_id,
                    backup_id=backup_id,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    kind=kind,
                    remote_key=remote_key,
                )
            )
            if replacement_mode and source is not None and source.protected:
                corrections.append(
                    ProtectedFlagCorrection(
                        profile_id=profile_id,
                        backup_id=backup_id,
                        target_protected=False,
                    )
                )

    if refusals:
        return ReconciliationPlan(
            classification=classification,
            profile_summary=summary,
            imports=(),
            protected_flag_corrections=(),
            prune_candidates=(),
            refusals=tuple(sorted(refusals)),
            remote_evidence=remote_evidence,
            remote_orphans=tuple(
                sorted(remote_orphans, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
            ),
        )
    return ReconciliationPlan(
        classification=classification,
        profile_summary=summary,
        imports=tuple(sorted(imports, key=lambda item: (_profile_order(item.profile_id), item.backup_id))),
        protected_flag_corrections=tuple(
            sorted(corrections, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
        ),
        prune_candidates=tuple(
            sorted(candidates, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
        ),
        remote_evidence=remote_evidence,
        replacement_candidates=tuple(
            sorted(replacements, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
        ),
        remote_orphans=tuple(
            sorted(remote_orphans, key=lambda item: (_profile_order(item.profile_id), item.backup_id))
        ),
    )


def apply_plan(
    connection: sqlite3.Connection,
    plan: ReconciliationPlan,
    *,
    now: datetime | None = None,
) -> None:
    """Apply only catalog transitions; never delete a local or remote object."""
    if not plan.ready:
        raise SafeError("backup_reconciliation_refused", "backup reconciliation was refused")
    if any(not item.target_protected for item in plan.protected_flag_corrections) and plan.prune_candidates:
        raise SafeError(
            "backup_reconciliation_refused",
            "prune protected transition requires exact replacement apply",
        )
    timestamp = _iso(now or datetime.now(timezone.utc))
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        _apply_catalog_transitions(connection, plan, timestamp)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def apply_replacement_plan(
    connection: sqlite3.Connection,
    transport: Any,
    plan: ReconciliationPlan,
    *,
    now: datetime | None = None,
) -> None:
    """Upload and verify missing locals before pruning exact remote objects."""
    if not plan.ready:
        raise SafeError("backup_reconciliation_refused", "backup reconciliation was refused")
    timestamp = _iso(now or datetime.now(timezone.utc))
    for item in plan.replacement_candidates:
        row = connection.execute(
            "SELECT profile_id,size_bytes,verified,protected FROM backups WHERE id=?",
            (item.backup_id,),
        ).fetchone()
        if row is None or tuple(row) != (item.profile_id, item.size_bytes, 1, 1):
            raise SafeError("backup_reconciliation_refused", "replacement catalog row changed")
    before = _fixed_inventory(transport)
    expected_before = {
        item.remote_key: item.size_bytes
        for item in plan.remote_evidence
        if item.remote_key
    }
    if before != expected_before:
        raise SafeError("backup_reconciliation_refused", "remote inventory changed before replacement")

    replacement_keys: dict[str, int] = {}
    for item in plan.replacement_candidates:
        source = item.archive_path
        if source is None or source.is_symlink() or not source.is_file():
            raise SafeError("backup_reconciliation_refused", "replacement archive is unavailable")
        actual_size, actual_sha = _local_archive_metadata(source)
        if actual_size != item.size_bytes or actual_sha != item.sha256:
            raise SafeError("backup_reconciliation_refused", "replacement archive changed")
        _validate_archive_manifest(source, item.profile_id, item.backup_id)
        key = _remote_key(item.profile_id, item.backup_id)
        existing_size = before.get(key)
        if existing_size is None:
            transport.upload(source, key)
        elif existing_size != item.size_bytes:
            raise SafeError("backup_reconciliation_refused", "replacement remote size changed")
        transport.verify(source, key)
        replacement_keys[key] = item.size_bytes

    after = _fixed_inventory(transport)
    expected_after = {**expected_before, **replacement_keys}
    if after != expected_after:
        raise SafeError("backup_reconciliation_refused", "remote inventory changed after replacement")

    for item in plan.prune_candidates:
        if not item.remote_key:
            raise SafeError("backup_reconciliation_refused", "prune candidate lacks canonical object")
        current_size = after.get(item.remote_key)
        if current_size is not None and current_size != item.size_bytes:
            raise SafeError("backup_reconciliation_refused", "prune candidate changed")
        if current_size is not None:
            transport.delete(item.remote_key)

    final_inventory = _fixed_inventory(transport)
    expected_final = {
        key: size
        for key, size in after.items()
        if key not in {item.remote_key for item in plan.prune_candidates}
    }
    if final_inventory != expected_final:
        raise SafeError("backup_reconciliation_refused", "remote prune could not be verified")

    try:
        connection.execute("BEGIN IMMEDIATE")
        _apply_catalog_transitions(
            connection,
            plan,
            timestamp,
            include_replacements=True,
            update_prunes=True,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _apply_catalog_transitions(
    connection: sqlite3.Connection,
    plan: ReconciliationPlan,
    timestamp: str,
    *,
    include_replacements: bool = False,
    update_prunes: bool = False,
) -> None:
    for item in plan.imports:
        _insert_protection(connection, item, timestamp)
    for item in plan.protected_flag_corrections:
        row = connection.execute(
            "SELECT protected,verified FROM backups WHERE id=? AND profile_id=?",
            (item.backup_id, item.profile_id),
        ).fetchone()
        if row is None:
            raise SafeError(
                "backup_reconciliation_refused",
                "catalog row changed before protected-flag correction",
            )
        current_protected = bool(row[0])
        if current_protected == item.target_protected:
            continue
        if not bool(row[1]):
            raise SafeError(
                "backup_reconciliation_refused",
                "unverified catalog row cannot change protected state",
            )
        updated = connection.execute(
            "UPDATE backups SET protected=? WHERE id=? AND profile_id=? AND protected=? AND verified=1",
            (
                int(item.target_protected),
                item.backup_id,
                item.profile_id,
                int(current_protected),
            ),
        )
        if updated.rowcount != 1:
            raise SafeError(
                "backup_reconciliation_refused",
                "protected flag changed before correction",
            )
    if include_replacements:
        for item in plan.replacement_candidates:
            _insert_protection(
                connection,
                ImportProtection(
                    profile_id=item.profile_id,
                    backup_id=item.backup_id,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    remote_key=_remote_key(item.profile_id, item.backup_id),
                ),
                timestamp,
            )
    if update_prunes:
        for item in plan.prune_candidates:
            connection.execute(
                "UPDATE backup_protections SET prune_state='deleted',updated_at=?,error_code=NULL "
                "WHERE backup_id=? AND profile_id=? AND destination_id=? AND backup_class=? AND remote_key=?",
                (
                    timestamp,
                    item.backup_id,
                    item.profile_id,
                    DESTINATION_ID,
                    BACKUP_CLASS,
                    item.remote_key,
                ),
            )


def _insert_protection(
    connection: sqlite3.Connection,
    item: ImportProtection,
    timestamp: str,
) -> None:
    row = connection.execute(
        "SELECT profile_id,remote_key,local_sha256,local_verified,upload_state,remote_verified,"
        "comparison_state,prune_state,error_code FROM backup_protections "
        "WHERE backup_id=? AND destination_id=? AND backup_class=?",
        (item.backup_id, DESTINATION_ID, BACKUP_CLASS),
    ).fetchone()
    if row is not None:
        if tuple(row) not in {
            (item.profile_id, item.remote_key, item.sha256, 1, "succeeded", 1, "verified", "not_started", None),
            (item.profile_id, item.remote_key, item.sha256, 1, "succeeded", 1, "verified", "succeeded", None),
        }:
            raise SafeError(
                "backup_reconciliation_refused",
                "existing protection row differs from proven evidence",
            )
        return
    connection.execute(
        "INSERT INTO backup_protections(backup_id,profile_id,destination_id,backup_class,remote_key,"
        "local_sha256,local_verified,upload_state,remote_verified,comparison_state,prune_state,updated_at,error_code) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.backup_id,
            item.profile_id,
            DESTINATION_ID,
            BACKUP_CLASS,
            item.remote_key,
            item.sha256,
            1,
            "succeeded",
            1,
            "verified",
            "not_started",
            timestamp,
            None,
        ),
    )


def build_fixed_plan(
    connection: sqlite3.Connection,
    transport: B2CommandTransport,
) -> ReconciliationPlan:
    """Capture the fixed catalog and remote scope without caller overrides."""
    _assert_fixed_profile_scope(connection)
    catalog: list[CatalogGeneration] = []
    placeholders = ",".join("?" for _ in RETAINED_PROFILE_IDS)
    rows = connection.execute(
        "SELECT id,profile_id,size_bytes,verified,protected FROM backups "
        f"WHERE profile_id IN ({placeholders}) ORDER BY profile_id,id",
        RETAINED_PROFILE_IDS,
    ).fetchall()
    for backup_id, profile_id, size_bytes, verified, protected in rows:
        profile_id = str(profile_id)
        backup_id = str(backup_id)
        path = FIXED_BACKUP_ROOTS[profile_id] / f"{backup_id}{ARCHIVE_SUFFIX}"
        actual_size, digest = _local_archive_metadata(path)
        if actual_size >= 0:
            _validate_archive_manifest(path, profile_id, backup_id)
        catalog.append(
            CatalogGeneration(
                backup_id=backup_id,
                profile_id=profile_id,
                size_bytes=int(size_bytes),
                sha256=digest,
                verified=bool(verified),
                protected=bool(protected),
                archive_size_bytes=actual_size,
                archive_path=path,
            )
        )

    protection_rows = connection.execute(
        "SELECT backup_id,profile_id,remote_key,local_sha256,local_verified,remote_verified,"
        "comparison_state,prune_state,upload_state,error_code FROM backup_protections "
        f"WHERE profile_id IN ({placeholders}) AND destination_id=? AND backup_class=?",
        (*RETAINED_PROFILE_IDS, DESTINATION_ID, BACKUP_CLASS),
    ).fetchall()
    protections = tuple(
        ProtectionGeneration(
            backup_id=str(row[0]),
            profile_id=str(row[1]),
            remote_key=str(row[2]),
            local_sha256=str(row[3]),
            local_verified=bool(row[4]),
            remote_verified=bool(row[5]),
            comparison_state=str(row[6]),
            prune_state=str(row[7]),
            upload_state=str(row[8]),
            error_code=row[9],
        )
        for row in protection_rows
    )
    catalogs = {(item.profile_id, item.backup_id): item for item in catalog}
    remote: list[RemoteGeneration] = []
    for profile_id in RETAINED_PROFILE_IDS:
        prefix = b2_prefix(profile_id)
        for item in transport.list(prefix):
            backup_id = _remote_backup_id(item, prefix)
            local = catalogs.get((profile_id, backup_id))
            digest = ""
            method = ""
            manifest_profile_id = None
            manifest_backup_id = None
            manifest_verified = False
            downloaded_size_bytes = None
            if local is not None and local.archive_path is not None and item.size_bytes == _archive_size(local):
                try:
                    transport.verify(local.archive_path, item.key)
                except SafeError:
                    digest = ""
                else:
                    digest = local.sha256
                    method = "cryptcheck"
            elif local is None:
                staged_path = FIXED_REMOTE_STAGING_ROOT / (
                    f"{profile_id}-{backup_id}{ARCHIVE_SUFFIX}"
                )
                if staged_path.is_file() and not staged_path.is_symlink():
                    (
                        manifest_profile_id,
                        manifest_backup_id,
                        digest,
                        downloaded_size_bytes,
                    ) = _verify_staged_manifest(
                        transport,
                        item,
                        profile_id,
                        backup_id,
                        staged_path,
                    )
                    method = "manifest+cryptcheck"
                else:
                    (
                        manifest_profile_id,
                        manifest_backup_id,
                        digest,
                        downloaded_size_bytes,
                    ) = _download_and_verify_manifest(transport, item, profile_id, backup_id)
                manifest_verified = True
            remote.append(
                RemoteGeneration(
                    profile_id=profile_id,
                    backup_id=backup_id,
                    size_bytes=int(item.size_bytes),
                    sha256=digest,
                    remote_key=item.key,
                    manifest_profile_id=manifest_profile_id,
                    manifest_backup_id=manifest_backup_id,
                    manifest_verified=manifest_verified,
                    downloaded_size_bytes=downloaded_size_bytes,
                    verification_method=method or "manifest",
                )
            )
    return reconcile(catalog, protections, remote, replacement_mode=True)


def _index_catalog(
    rows: Iterable[CatalogGeneration], refusals: set[str]
) -> dict[str, dict[str, CatalogGeneration]]:
    result = {profile_id: {} for profile_id in RETAINED_PROFILE_IDS}
    for item in rows:
        if item.profile_id not in result:
            refusals.add("unknown catalog profile")
            continue
        if not GENERATION_RE.fullmatch(item.backup_id):
            refusals.add("catalog generation ID is invalid")
            continue
        if item.backup_id in result[item.profile_id]:
            refusals.add("duplicate catalog generation")
            continue
        result[item.profile_id][item.backup_id] = item
    return result


def _assert_fixed_profile_scope(connection: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _ in RETAINED_PROFILE_IDS)
    unknown_backup = connection.execute(
        "SELECT 1 FROM backups "
        f"WHERE profile_id NOT IN ({placeholders}) LIMIT 1",
        RETAINED_PROFILE_IDS,
    ).fetchone()
    unknown_protection = connection.execute(
        "SELECT 1 FROM backup_protections WHERE destination_id=? AND backup_class=? "
        f"AND profile_id NOT IN ({placeholders}) LIMIT 1",
        (DESTINATION_ID, BACKUP_CLASS, *RETAINED_PROFILE_IDS),
    ).fetchone()
    if unknown_backup is not None or unknown_protection is not None:
        raise SafeError("backup_reconciliation_refused", "fixed profile scope is not exact")


def _manifest_proves_orphan(
    item: RemoteGeneration,
    profile_id: str,
    backup_id: str,
) -> bool:
    return bool(
        item.manifest_verified
        and item.manifest_profile_id == profile_id
        and item.manifest_backup_id == backup_id
        and item.downloaded_size_bytes == item.size_bytes
        and SHA256_RE.fullmatch(item.sha256)
        and item.verification_method in {"manifest", "manifest+cryptcheck"}
    )


def _protection_proves_cryptcheck(
    protection: ProtectionGeneration | None,
    catalog: CatalogGeneration,
    remote: RemoteGeneration | None,
) -> bool:
    return bool(
        protection is not None
        and remote is not None
        and protection.remote_key == _remote_key(catalog.profile_id, catalog.backup_id)
        and protection.local_sha256 == catalog.sha256
        and protection.local_verified
        and protection.remote_verified
        and protection.comparison_state == "verified"
        and protection.upload_state == "succeeded"
        and protection.prune_state in {"not_started", "succeeded"}
        and protection.error_code is None
        and remote.remote_key == _remote_key(catalog.profile_id, catalog.backup_id)
        and remote.size_bytes == _archive_size(catalog)
        and remote.sha256 == catalog.sha256
        and remote.verification_method in {"cryptcheck", "manifest+cryptcheck"}
    )


def _deleted_protection_proves_prune(
    protection: ProtectionGeneration,
    catalog: CatalogGeneration,
) -> bool:
    return bool(
        protection.profile_id == catalog.profile_id
        and protection.backup_id == catalog.backup_id
        and protection.remote_key == _remote_key(catalog.profile_id, catalog.backup_id)
        and protection.local_sha256 == catalog.sha256
        and protection.local_verified
        and protection.remote_verified
        and protection.comparison_state == "verified"
        and protection.upload_state == "succeeded"
        and protection.prune_state == "deleted"
        and protection.error_code is None
    )


def _download_and_verify_manifest(
    transport: Any,
    item: RemoteObject,
    profile_id: str,
    backup_id: str,
) -> tuple[str, str, str, int]:
    if item.size_bytes < 0 or item.size_bytes > MAX_STAGED_BYTES:
        raise SafeError("backup_reconciliation_refused", "remote archive exceeds staging bound")
    try:
        with tempfile.TemporaryDirectory(prefix=".horizon-b2-reconcile-") as directory:
            destination = Path(directory) / f"{backup_id}{ARCHIVE_SUFFIX}"
            transport.download(item.key, destination)
            actual_size, digest = _local_archive_metadata(destination)
            if actual_size != item.size_bytes or not SHA256_RE.fullmatch(digest):
                raise SafeError("backup_reconciliation_refused", "downloaded remote archive changed")
            manifest = _read_manifest(destination)
            manifest_profile_id = manifest.get("profile_id")
            manifest_backup_id = manifest.get("backup_id")
            if (
                manifest.get("schema") != 1
                or manifest_profile_id != profile_id
                or manifest_backup_id != backup_id
            ):
                raise SafeError("backup_reconciliation_refused", "remote manifest identity mismatch")
            return str(manifest_profile_id), str(manifest_backup_id), digest, actual_size
    except SafeError:
        raise
    except (OSError, subprocess.SubprocessError, tarfile.TarError, ValueError, KeyError) as exc:
        raise SafeError("backup_reconciliation_refused", "remote manifest could not be verified") from exc


def _verify_staged_manifest(
    transport: Any,
    item: RemoteObject,
    profile_id: str,
    backup_id: str,
    path: Path,
) -> tuple[str, str, str, int]:
    actual_size, digest = _local_archive_metadata(path)
    if actual_size != item.size_bytes or not SHA256_RE.fullmatch(digest):
        raise SafeError("backup_reconciliation_refused", "staged remote archive changed")
    _validate_archive_manifest(path, profile_id, backup_id)
    # The fixed evidence staging name includes the profile to avoid collisions,
    # while rclone cryptcheck requires the local basename to match the canonical
    # remote object.  Verify through a same-filesystem hard link so multi-GiB
    # evidence is neither copied nor renamed.
    try:
        with tempfile.TemporaryDirectory(prefix=".horizon-cryptcheck-", dir=path.parent) as directory:
            canonical = Path(directory) / item.key.rsplit("/", 1)[-1]
            canonical.hardlink_to(path)
            if not canonical.samefile(path):
                raise SafeError("backup_reconciliation_refused", "staged remote archive changed")
            transport.verify(canonical, item.key)
    except SafeError:
        raise
    except OSError as exc:
        raise SafeError("backup_reconciliation_refused", "staged remote archive could not be verified") from exc
    return profile_id, backup_id, digest, actual_size


def _validate_archive_manifest(path: Path, profile_id: str, backup_id: str) -> None:
    manifest = _read_manifest(path)
    if (
        manifest.get("schema") != 1
        or manifest.get("profile_id") != profile_id
        or manifest.get("backup_id") != backup_id
    ):
        raise SafeError("backup_reconciliation_refused", "local manifest identity mismatch")


def _read_manifest(path: Path) -> dict[str, Any]:
    payload: bytes
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.name == "manifest.json"]
            if len(members) != 1 or not members[0].isfile():
                raise SafeError("backup_reconciliation_refused", "backup manifest is not unique")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise SafeError("backup_reconciliation_refused", "backup manifest is unreadable")
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
    except SafeError:
        raise
    except (OSError, tarfile.TarError):
        try:
            with subprocess.Popen(
                [
                    "/usr/bin/tar",
                    "--zstd",
                    "--extract",
                    "--to-stdout",
                    "--file",
                    str(path),
                    "manifest.json",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as process:
                if process.stdout is None:
                    raise OSError("manifest stream is unavailable")
                payload = process.stdout.read(MAX_MANIFEST_BYTES + 1)
                if len(payload) > MAX_MANIFEST_BYTES:
                    process.kill()
                try:
                    returncode = process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                if len(payload) <= MAX_MANIFEST_BYTES and returncode != 0:
                    raise subprocess.CalledProcessError(returncode, process.args)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SafeError("backup_reconciliation_refused", "backup manifest is unreadable") from exc
    if len(payload) > MAX_MANIFEST_BYTES:
        raise SafeError("backup_reconciliation_refused", "backup manifest exceeds bound")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeError("backup_reconciliation_refused", "backup manifest is invalid") from exc
    if not isinstance(value, dict):
        raise SafeError("backup_reconciliation_refused", "backup manifest is invalid")
    return value


def _fixed_inventory(transport: Any) -> dict[str, int]:
    inventory: dict[str, int] = {}
    for profile_id in RETAINED_PROFILE_IDS:
        prefix = b2_prefix(profile_id)
        for item in transport.list(prefix):
            backup_id = _remote_backup_id(item, prefix)
            if not GENERATION_RE.fullmatch(backup_id) or item.key != _remote_key(profile_id, backup_id):
                raise SafeError("backup_reconciliation_refused", "remote inventory contains an invalid object")
            if item.size_bytes < 0 or item.size_bytes > MAX_STAGED_BYTES:
                raise SafeError("backup_reconciliation_refused", "remote inventory size is invalid")
            if item.key in inventory:
                raise SafeError("backup_reconciliation_refused", "remote inventory contains a duplicate")
            inventory[item.key] = item.size_bytes
    return inventory


def _profile_order(profile_id: str) -> int:
    try:
        return RETAINED_PROFILE_IDS.index(profile_id)
    except ValueError:
        return len(RETAINED_PROFILE_IDS)


def _index_protections(
    rows: Iterable[ProtectionGeneration],
    catalogs: dict[str, dict[str, CatalogGeneration]],
    refusals: set[str],
) -> dict[str, dict[str, ProtectionGeneration]]:
    result = {profile_id: {} for profile_id in RETAINED_PROFILE_IDS}
    for item in rows:
        if item.profile_id not in result:
            refusals.add("unknown protection profile")
            continue
        if not GENERATION_RE.fullmatch(item.backup_id):
            refusals.add("protection generation ID is invalid")
            continue
        if item.prune_state == "deleted":
            continue
        if item.backup_id in result[item.profile_id]:
            refusals.add("duplicate protection row")
            continue
        result[item.profile_id][item.backup_id] = item
    return result


def _index_remote(
    rows: Iterable[RemoteGeneration],
    catalogs: dict[str, dict[str, CatalogGeneration]],
    refusals: set[str],
) -> dict[str, dict[str, RemoteGeneration]]:
    result = {profile_id: {} for profile_id in RETAINED_PROFILE_IDS}
    for item in rows:
        if item.profile_id not in result:
            refusals.add("unknown remote profile")
            continue
        if not GENERATION_RE.fullmatch(item.backup_id):
            refusals.add("remote generation ID is invalid")
            continue
        if item.backup_id in result[item.profile_id]:
            refusals.add("duplicate remote generation")
            continue
        result[item.profile_id][item.backup_id] = item
    return result


def _remote_backup_id(item: RemoteObject, prefix: str) -> str:
    key = item.key
    if not key.startswith(prefix + "/") or key.count("/") != prefix.count("/") + 1:
        return "invalid-remote-key"
    basename = key.rsplit("/", 1)[-1]
    if not basename.endswith(ARCHIVE_SUFFIX):
        return "invalid-remote-key"
    return basename[: -len(ARCHIVE_SUFFIX)]


def _remote_key(profile_id: str, backup_id: str) -> str:
    return f"{b2_prefix(profile_id)}/{backup_id}{ARCHIVE_SUFFIX}"


def _archive_size(item: CatalogGeneration) -> int:
    return item.archive_size_bytes if item.archive_size_bytes is not None else item.size_bytes


def _generation_sort_key(backup_id: str) -> tuple[str, str]:
    return backup_id.split("-", 1)[0], backup_id


def _local_archive_metadata(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        return -1, ""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path.stat().st_size, digest.hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def plan_json(plan: ReconciliationPlan) -> str:
    return json.dumps(plan.to_public_dict(), sort_keys=True, separators=(",", ":"))
