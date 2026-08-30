from pathlib import Path
import hashlib
import json
import os
import sqlite3

import pytest

from game_control.managed_tuning import (
    ManagedTuningError,
    activate_managed_argfile,
    quiesce_capability,
    readiness_event,
    rollback_managed_argfile,
    validate_slice_policy,
    verify_managed_argfile,
)


def _campaign():
    return {"schemaVersion": 2, "overallVerdict": "better", "driverSha256": "a" * 64, "configSha256": "b" * 64, "profileId": "minecraft-sunlit-cobblemon", "baselinePreset": "current", "candidatePreset": "candidate"}


def test_slice_policy_preserves_approved_ceilings_and_rejects_io_max():
    validate_slice_policy("MemoryHigh=1G\nMemoryMax=2G", "MemoryHigh=2G\nMemoryMax=3G", "MemoryHigh=9G\nMemoryMax=10G", "MemoryHigh=8G\nMemoryMax=9G")
    with pytest.raises(ManagedTuningError):
        validate_slice_policy("MemoryHigh=1G\nMemoryMax=2G\nio.max=x", "MemoryHigh=2G\nMemoryMax=3G", "MemoryHigh=9G\nMemoryMax=10G", "MemoryHigh=8G\nMemoryMax=9G")


def test_quiesce_capability_is_ext4_only_and_makes_no_fake_claims():
    report = quiesce_capability("/dev/vda /srv ext4 rw,relatime 0 0\n", "/srv")
    assert report["filesystem"] == "ext4"
    assert report["hardlink"] is False and report["snapshot"] is False
    assert report["post_quiesce_transport"] == "maintenance.slice"
    report = quiesce_capability("/dev/vda /srv xfs rw 0 0\n", "/srv/world")
    assert report["filesystem"] == "xfs" and report["offline"] is True and report["ext4_verified"] is False


def test_managed_argfile_requires_accepted_campaign_and_is_verifiable(tmp_path: Path):
    path = tmp_path / "active.args"
    campaign = _campaign()
    import hashlib, json
    expected = hashlib.sha256(json.dumps(campaign, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = activate_managed_argfile(path, ["-XX:+UseG1GC", "-Xms4G"], campaign=campaign, expected_campaign_digest=expected)
    assert verify_managed_argfile(record)
    assert path.read_text() == "-XX:+UseG1GC\n-Xms4G\n"
    with pytest.raises(ManagedTuningError):
        activate_managed_argfile(tmp_path / "bad.args", ["-Xmx4G"], campaign={"schemaVersion": 2, "overallVerdict": "inconclusive"}, expected_campaign_digest="0" * 64)
    rollback = tmp_path / "rollback.args"
    rollback.write_text("-Xms2G\n")
    rollback.chmod(0o644)
    rollback_managed_argfile(path, rollback)
    assert path.read_text() == "-Xms2G\n"


def test_readiness_event_bounds_wake_duration():
    event = readiness_event("minecraft-sunlit-cobblemon", 1234, True)
    assert event.source == "start-health"
    with pytest.raises(ManagedTuningError):
        readiness_event("minecraft-sunlit-cobblemon", 900001, True)


def _accepted_fixture(tmp_path: Path, *, artifact_name="report.json", summary=None):
    root = tmp_path / "reports"; root.mkdir(mode=0o700, parents=True)
    artifact = root / artifact_name
    payload = {"schemaVersion": 2, "overallVerdict": "better",
               "baselinePreset": "current", "candidatePreset": "candidate",
               "profileId": "minecraft-sunlit-cobblemon",
               "driverSha256": "a" * 64, "configSha256": "b" * 64}
    artifact.write_text(json.dumps(payload), encoding="utf-8"); artifact.chmod(0o644)
    db = tmp_path / "bench.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE benchmark_runs (profile_id TEXT, baseline_preset TEXT, candidate_preset TEXT, state TEXT, overall_verdict TEXT, finished_at TEXT, artifact_path TEXT, artifact_sha256 TEXT, summary_json TEXT)")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    connection.execute("INSERT INTO benchmark_runs VALUES (?,?,?,?,?,?,?,?,?)",
                       ("minecraft-sunlit-cobblemon", "current", "candidate", "succeeded",
                        "better", "2026-01-01", artifact_name, digest,
                        json.dumps(summary if summary is not None else payload)))
    connection.commit(); connection.close(); db.chmod(0o600)
    return db, root, digest


def test_accepted_campaign_loader_is_bounded_contained_and_closes_db(tmp_path: Path):
    from game_control.managed_tuning import load_accepted_campaign
    db, root, digest = _accepted_fixture(tmp_path)
    campaign, _ = load_accepted_campaign(db, profile_id="minecraft-sunlit-cobblemon",
                                         baseline="current", candidate="candidate",
                                         artifact_digest=digest, artifact_root=root)
    assert campaign["profileId"] == "minecraft-sunlit-cobblemon"
    outside = tmp_path / "outside.json"; outside.write_text("{}"); outside.chmod(0o644)
    db2, root2, digest2 = _accepted_fixture(tmp_path / "second")
    (root2 / "escape").symlink_to(outside)
    connection = sqlite3.connect(db2)
    connection.execute("UPDATE benchmark_runs SET artifact_path='escape'")
    connection.commit(); connection.close()
    with pytest.raises(ManagedTuningError):
        load_accepted_campaign(db2, profile_id="minecraft-sunlit-cobblemon",
                               baseline="current", candidate="candidate",
                               artifact_digest=digest2, artifact_root=root2)


@pytest.mark.parametrize("kind", ["oversized", "malformed", "array", "scalar", "wrong_mode", "relative_escape"])
def test_accepted_campaign_loader_refuses_forged_artifacts(tmp_path: Path, kind: str):
    from game_control.managed_tuning import load_accepted_campaign, MAX_CAMPAIGN_ARTIFACT_BYTES
    db, root, digest = _accepted_fixture(tmp_path)
    artifact = root / "report.json"
    if kind == "oversized":
        artifact.write_bytes(b"x" * (MAX_CAMPAIGN_ARTIFACT_BYTES + 1))
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    elif kind == "malformed":
        artifact.write_text("{", encoding="utf-8")
    elif kind == "array":
        artifact.write_text("[]", encoding="utf-8")
    elif kind == "scalar":
        artifact.write_text("1", encoding="utf-8")
    elif kind == "wrong_mode":
        artifact.chmod(0o666)
    else:
        connection = sqlite3.connect(db)
        connection.execute("UPDATE benchmark_runs SET artifact_path='../report.json'")
        connection.commit(); connection.close()
    with pytest.raises(ManagedTuningError):
        load_accepted_campaign(db, profile_id="minecraft-sunlit-cobblemon",
                               baseline="current", candidate="candidate",
                               artifact_digest=digest, artifact_root=root)


def test_jvm_cli_activate_verify_rollback_uses_canonical_row(tmp_path: Path, monkeypatch):
    import importlib.util
    from importlib.machinery import SourceFileLoader
    cli = SourceFileLoader("horizon_jvm_args", str(Path(__file__).parents[1] / "ops/bin/horizon-jvm-args")).load_module()
    db, root, digest = _accepted_fixture(tmp_path)
    target_dir = tmp_path / "jvm"; target_dir.mkdir(mode=0o700)
    target = target_dir / "active.args"; rollback = target_dir / "rollback.args"
    target.write_text("-Xms2G\n"); target.chmod(0o644)
    candidate = tmp_path / "candidate.args"; candidate.write_text("-XX:+UseG1GC\n-Xms4G\n"); candidate.chmod(0o644)
    summary = tmp_path / "summary.json"; summary.write_text(json.dumps({"baselinePreset": "current", "candidatePreset": "candidate"})); summary.chmod(0o644)
    accepted = tmp_path / "accepted.digest"; accepted.write_text(digest); accepted.chmod(0o644)
    for name, value in {"TARGET": target, "MANIFEST": target.with_suffix(".args.manifest.json"),
                        "ROLLBACK": rollback, "CAMPAIGN": summary, "BENCHMARK_DB": db,
                        "CANDIDATE": candidate, "ACCEPTED_DIGEST": accepted,
                        "ARTIFACT_ROOT": root}.items():
        monkeypatch.setattr(cli, name, value)
    assert cli.main(["activate"]) == 0
    assert cli.main(["verify"]) == 0
    assert target.read_text() == candidate.read_text()
    assert cli.main(["rollback"]) == 0
    assert target.read_text() == "-Xms2G\n"
    candidate.write_text("-javaagent:forged.jar\n")
    assert cli.main(["activate"]) == 1
