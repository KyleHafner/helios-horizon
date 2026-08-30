from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from game_control import state_db
from game_control.managed_tuning import ManagedTuningError, load_accepted_campaign


def _db(path: Path, artifact: Path, digest: str, summary: dict[str, object]) -> None:
    connection = sqlite3.connect(path)
    state_db._configure(connection)
    for statement in state_db._STATE_TABLES:
        connection.execute(statement)
    connection.execute("PRAGMA user_version=4")
    connection.execute(
        "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json,artifact_path,artifact_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run-1", "minecraft-sunlit-cobblemon", "current", "candidate", "succeeded",
         "2026-08-23T00:00:00Z", "2026-08-23T00:01:00Z", "better", json.dumps(summary), artifact.name, digest),
    )
    connection.commit()
    connection.close()


def test_load_accepted_campaign_binds_canonical_row_and_artifact(tmp_path: Path):
    artifact_root = tmp_path / "reports"
    artifact_root.mkdir()
    artifact = artifact_root / "summary.json"
    payload = {
        "schemaVersion": 2,
        "baselinePreset": "current",
        "candidatePreset": "candidate",
        "overallVerdict": "better",
    }
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    db = tmp_path / "state.db"
    _db(db, artifact, digest, payload)

    campaign, campaign_digest = load_accepted_campaign(
        db, profile_id="minecraft-sunlit-cobblemon", baseline="current",
        candidate="candidate", artifact_digest=digest, artifact_root=artifact_root,
    )
    assert campaign["overallVerdict"] == "better"
    assert len(campaign_digest) == 64


@pytest.mark.parametrize("forged", ["digest", "summary"])
def test_load_accepted_campaign_refuses_forged_digest_or_summary(tmp_path: Path, forged: str):
    artifact_root = tmp_path / "reports"
    artifact_root.mkdir()
    artifact = artifact_root / "summary.json"
    payload = {
        "schemaVersion": 2,
        "baselinePreset": "current",
        "candidatePreset": "candidate",
        "overallVerdict": "better",
    }
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    db = tmp_path / "state.db"
    row_summary = dict(payload)
    if forged == "summary":
        row_summary["overallVerdict"] = "better"
        artifact.write_text(json.dumps({**payload, "overallVerdict": "worse"}), encoding="utf-8")
    _db(db, artifact, digest, row_summary)
    supplied = "0" * 64 if forged == "digest" else digest
    with pytest.raises(ManagedTuningError):
        load_accepted_campaign(
            db, profile_id="minecraft-sunlit-cobblemon", baseline="current",
            candidate="candidate", artifact_digest=supplied, artifact_root=artifact_root,
        )
