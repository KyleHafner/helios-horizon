"""Offline policy gates for managed JVM tuning and wake readiness evidence.

This module deliberately does not edit a modpack's user JVM argument file or
invoke systemd.  It validates package policy and provides an atomic, reversible
root-owned generated argfile boundary for an explicitly accepted campaign.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class ManagedTuningError(ValueError):
    pass


MAX_MANAGED_ARGS = 16
MAX_MANAGED_BYTES = 4096
MAX_CAMPAIGN_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_CAMPAIGN_SUMMARY_BYTES = 2 * 1024 * 1024


def _read_secure_file(path: Path, *, max_bytes: int, label: str) -> bytes:
    """Read one root-owned, single-link, non-writable regular file without following links."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o022 or info.st_nlink != 1
                    or info.st_size > max_bytes):
                raise ManagedTuningError(f"{label} is not a secure bounded regular file")
            chunks: list[bytes] = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    raise ManagedTuningError(f"{label} changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)
    except ManagedTuningError:
        raise
    except OSError as exc:
        raise ManagedTuningError(f"{label} is unavailable") from exc


def _secure_db_path(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o022 or info.st_nlink != 1):
                raise ManagedTuningError("benchmark database is not a secure regular file")
        finally:
            os.close(fd)
    except ManagedTuningError:
        raise
    except OSError as exc:
        raise ManagedTuningError("benchmark database is unavailable") from exc


def validate_slice_policy(horizon: str, maintenance: str, games_slice: str, sunlit_service: str) -> None:
    if "MemoryHigh=1G" not in horizon or "MemoryMax=2G" not in horizon:
        raise ManagedTuningError("slice ceilings are not the approved bounded policy")
    if "MemoryHigh=2G" not in maintenance or "MemoryMax=3G" not in maintenance:
        raise ManagedTuningError("maintenance.slice policy is not the approved 2G/3G ceiling")
    if "MemoryHigh=9G" not in games_slice or "MemoryMax=10G" not in games_slice:
        raise ManagedTuningError("games.slice policy is not the approved 9G/10G ceiling")
    if "MemoryHigh=8G" not in sunlit_service or "MemoryMax=9G" not in sunlit_service:
        raise ManagedTuningError("Sunlit service policy is not the approved 8G/9G ceiling")
    if "io.max" in horizon + maintenance + games_slice + sunlit_service:
        raise ManagedTuningError("unverified io.max claim is forbidden")


def quiesce_capability(mounts_text: str, mountpoint: str) -> dict[str, object]:
    """Report verified filesystem capability; never claims snapshots or links."""
    target = Path(mountpoint).resolve()
    candidates: list[tuple[int, str, str]] = []
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mount = Path(fields[1]).resolve()
            try:
                target.relative_to(mount)
            except ValueError:
                continue
            candidates.append((len(str(mount)), fields[2], str(mount)))
    if not candidates:
        raise ManagedTuningError("mount capability is unavailable or ambiguous")
    longest = max(item[0] for item in candidates)
    selected = [item for item in candidates if item[0] == longest]
    if len({(item[1], item[2]) for item in selected}) != 1:
        raise ManagedTuningError("mount capability is ambiguous")
    _, filesystem, containing_mount = selected[0]
    return {
        "filesystem": filesystem,
        "mountpoint": containing_mount,
        "offline": True,
        "ext4_verified": filesystem == "ext4",
        "hardlink": False,
        "snapshot": False,
        "post_quiesce_transport": "maintenance.slice",
    }


def _canonical_args(args: Sequence[str]) -> bytes:
    values = list(args)
    allowed_prefixes = ("-Xms", "-Xmx", "-Xss", "-XX:MaxGCPauseMillis=", "-XX:ActiveProcessorCount=")
    exact = {"-XX:+UseG1GC"}
    if not values or len(values) > MAX_MANAGED_ARGS or any(
        not isinstance(value, str) or not value.startswith(allowed_prefixes)
        and value not in exact
        or any(char.isspace() for char in value) or any(char in value for char in ("\n", "\r", "\x00"))
        or value.startswith(("@", "-javaagent", "-agentlib", "-agentpath"))
        or "/" in value or "\\" in value
        for value in values
    ):
        raise ManagedTuningError("managed JVM args must be bounded JVM flags")
    payload = ("\n".join(values) + "\n").encode("utf-8")
    if len(payload) > MAX_MANAGED_BYTES:
        raise ManagedTuningError("managed JVM args exceed bounded size")
    return payload


@dataclass(frozen=True)
class ManagedJvmArgfile:
    path: Path
    digest: str
    campaign_digest: str
    manifest: Path
    precedence: str = "user_jvm_args.txt,managed-active.args,forge-unix_args.txt"


def _campaign_digest(campaign: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(campaign, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_accepted_campaign(db_path: Path, *, profile_id: str, baseline: str, candidate: str, artifact_digest: str, artifact_root: Path | None = None) -> tuple[dict[str, object], str]:
    """Load acceptance from the canonical benchmark row, never a self-asserted summary."""
    connection: sqlite3.Connection | None = None
    try:
        _secure_db_path(db_path)
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT state, baseline_preset, candidate_preset, overall_verdict, artifact_path, artifact_sha256, summary_json FROM benchmark_runs WHERE profile_id=? AND baseline_preset=? AND candidate_preset=? AND state='succeeded' AND overall_verdict='better' ORDER BY finished_at DESC LIMIT 1", (profile_id, baseline, candidate)).fetchone()
        root = artifact_root or Path("/srv/game-servers/minecraft-sunlit-benchmark/.horizon-reports")
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink() or root_info.st_uid != os.geteuid():
            raise ManagedTuningError("benchmark artifact root is not secure")
        relative = Path(row[4]) if row else Path()
        if row is None or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ManagedTuningError("accepted benchmark provenance is unavailable")
        artifact = (root / relative).resolve()
        try:
            artifact.relative_to(root.resolve())
        except ValueError as exc:
            raise ManagedTuningError("benchmark artifact escapes its root") from exc
        artifact_bytes = _read_secure_file(artifact, max_bytes=MAX_CAMPAIGN_ARTIFACT_BYTES, label="benchmark artifact")
        if row[5] != artifact_digest or hashlib.sha256(artifact_bytes).hexdigest() != artifact_digest:
            raise ManagedTuningError("accepted benchmark provenance is unavailable")
        artifact_campaign = json.loads(artifact_bytes.decode("utf-8"))
        if not isinstance(artifact_campaign, dict):
            raise ManagedTuningError("accepted benchmark artifact is invalid")
        if (artifact_campaign.get("schemaVersion") != 2 or artifact_campaign.get("overallVerdict") != "better"
                or artifact_campaign.get("baselinePreset") != baseline or artifact_campaign.get("candidatePreset") != candidate):
            raise ManagedTuningError("accepted benchmark artifact provenance is invalid")
        summary = row[6]
        if not isinstance(summary, str) or len(summary.encode("utf-8")) > MAX_CAMPAIGN_SUMMARY_BYTES:
            raise ManagedTuningError("accepted benchmark summary is too large")
        # summary_json is intentionally only diagnostics in the live schema;
        # campaign provenance comes from the hashed raw v2 artifact.
        campaign = artifact_campaign
        campaign["profileId"] = profile_id
        campaign["baselinePreset"] = baseline
        campaign["candidatePreset"] = candidate
        campaign["overallVerdict"] = "better"
        return campaign, _campaign_digest(campaign)
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ManagedTuningError("accepted benchmark provenance is unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def activate_managed_argfile(
    path: Path,
    args: Sequence[str],
    *,
    campaign: Mapping[str, object],
    expected_campaign_digest: str,
    profile_id: str = "minecraft-sunlit-cobblemon",
    baseline_preset: str | None = None,
    candidate_preset: str | None = None,
    rollback_path: Path | None = None,
) -> ManagedJvmArgfile:
    """Atomically activate only an accepted tuning campaign's generated args."""
    verdict = campaign.get("overallVerdict")
    required = ("profileId", "baselinePreset", "candidatePreset", "driverSha256", "configSha256")
    if verdict != "better" or campaign.get("schemaVersion") not in {2, "2"} or any(not campaign.get(key) for key in required):
        raise ManagedTuningError("managed tuning requires an accepted tuning campaign")
    if campaign.get("profileId") != profile_id or (baseline_preset is not None and campaign.get("baselinePreset") != baseline_preset) or (candidate_preset is not None and campaign.get("candidatePreset") != candidate_preset):
        raise ManagedTuningError("campaign profile or preset provenance mismatch")
    digest = _campaign_digest(campaign)
    if len(expected_campaign_digest) != 64 or digest != expected_campaign_digest:
        raise ManagedTuningError("campaign provenance digest mismatch")
    payload = _canonical_args(args)
    parent = path.parent
    info = parent.stat()
    if parent.is_symlink() or path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ManagedTuningError("managed JVM argfile parent is not private and owned")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ManagedTuningError("managed JVM argfile target is not a regular file")
    previous = path.read_bytes() if path.exists() else None
    manifest = path.with_suffix(path.suffix + ".manifest.json")
    if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
        raise ManagedTuningError("managed JVM manifest target is not a regular file")
    previous_manifest = manifest.read_bytes() if manifest.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o644)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        os.replace(temp, path)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        manifest_payload = json.dumps({"schemaVersion": 1, "profileId": profile_id, "baselinePreset": baseline_preset, "candidatePreset": candidate_preset, "overallVerdict": "better", "argfileSha256": hashlib.sha256(payload).hexdigest(), "campaignSha256": digest}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        mfd, mname = tempfile.mkstemp(prefix=f".{manifest.name}.", dir=parent)
        with os.fdopen(mfd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644); stream.write(manifest_payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(mname, manifest)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        if rollback_path is not None:
            if rollback_path.is_symlink() or rollback_path.parent != parent:
                raise ManagedTuningError("rollback path is outside the managed parent")
            if previous is not None:
                rollback_path.write_bytes(previous); os.chmod(rollback_path, 0o644)
                rfd = os.open(rollback_path, os.O_RDONLY); os.fsync(rfd); os.close(rfd)
                if previous_manifest is not None:
                    rollback_manifest = rollback_path.with_suffix(rollback_path.suffix + ".manifest.json")
                    rollback_manifest.write_bytes(previous_manifest); os.chmod(rollback_manifest, 0o644)
                    rfd = os.open(rollback_manifest, os.O_RDONLY); os.fsync(rfd); os.close(rfd)
        return ManagedJvmArgfile(path, hashlib.sha256(payload).hexdigest(), digest, manifest)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        if path.exists() and path.is_file():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                recovery = parent / f".{path.name}.recovery"
                recovery.write_bytes(previous); os.chmod(recovery, 0o644); os.replace(recovery, path)
        if previous_manifest is None:
            manifest.unlink(missing_ok=True)
        else:
            manifest.write_bytes(previous_manifest); os.chmod(manifest, 0o644)
        raise


def verify_managed_argfile(record: ManagedJvmArgfile) -> bool:
    try:
        parent = record.path.parent
        pinfo = parent.lstat(); info = record.path.lstat(); minfo = record.manifest.lstat()
        if parent.is_symlink() or not stat.S_ISDIR(pinfo.st_mode) or pinfo.st_uid != os.geteuid() or stat.S_IMODE(pinfo.st_mode) != 0o700:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o644 or info.st_size > MAX_MANAGED_BYTES:
            return False
        if not stat.S_ISREG(minfo.st_mode) or minfo.st_uid != os.geteuid() or stat.S_IMODE(minfo.st_mode) != 0o644 or minfo.st_size > 2048:
            return False
        evidence = json.loads(record.manifest.read_text(encoding="utf-8"))
        return evidence.get("argfileSha256") == record.digest and evidence.get("campaignSha256") == record.campaign_digest and hashlib.sha256(record.path.read_bytes()).hexdigest() == record.digest
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def rollback_managed_argfile(path: Path, rollback_path: Path | None) -> None:
    """Restore the previously active bytes with the same atomic boundary."""
    if rollback_path is None:
        if path.is_symlink(): raise ManagedTuningError("managed JVM target is a symlink")
        path.unlink(missing_ok=True)
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        manifest.unlink(missing_ok=True)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try: os.fsync(dir_fd)
        finally: os.close(dir_fd)
        return
    if not rollback_path.is_file() or rollback_path.is_symlink():
        raise ManagedTuningError("managed JVM rollback evidence is unavailable")
    if stat.S_IMODE(rollback_path.stat().st_mode) != 0o644:
        raise ManagedTuningError("managed JVM rollback evidence is insecure")
    parent = path.parent
    parent_info = parent.lstat()
    if parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid() or stat.S_IMODE(parent_info.st_mode) != 0o700:
        raise ManagedTuningError("managed JVM rollback parent is insecure")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(rollback_path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        rollback_manifest = rollback_path.with_suffix(rollback_path.suffix + ".manifest.json")
        active_manifest = path.with_suffix(path.suffix + ".manifest.json")
        if rollback_manifest.is_file() and not rollback_manifest.is_symlink():
            active_manifest.write_bytes(rollback_manifest.read_bytes()); os.chmod(active_manifest, 0o644)
        else:
            active_manifest.unlink(missing_ok=True)
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class WakeReadinessEvent:
    profile_id: str
    duration_ms: int
    healthy: bool
    source: str = "start-health"


def readiness_event(profile_id: str, duration_ms: int, healthy: bool) -> WakeReadinessEvent:
    if not profile_id or not isinstance(duration_ms, int) or not 0 <= duration_ms <= 900_000:
        raise ManagedTuningError("wake duration is outside the bounded SLO range")
    return WakeReadinessEvent(profile_id, duration_ms, bool(healthy))


__all__ = [
    "ManagedJvmArgfile", "ManagedTuningError", "WakeReadinessEvent",
    "activate_managed_argfile", "quiesce_capability", "readiness_event",
    "load_accepted_campaign",
    "rollback_managed_argfile",
    "validate_slice_policy", "verify_managed_argfile",
]
