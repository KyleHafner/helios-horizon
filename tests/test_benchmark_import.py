from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

UTILITY_PATH = Path(__file__).parents[1] / "ops/bin/horizon-benchmark-import"
loader = importlib.machinery.SourceFileLoader("horizon_benchmark_import", str(UTILITY_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


def test_installed_helper_uses_horizon_virtualenv():
    assert UTILITY_PATH.read_text(encoding="utf-8").startswith(
        "#!/opt/game-control/.venv/bin/python\n"
    )


def _db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE benchmark_runs(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,baseline_preset TEXT NOT NULL,candidate_preset TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,finished_at TEXT,overall_verdict TEXT,summary_json TEXT,artifact_path TEXT,error_code TEXT)")
    db.commit(); db.close(); path.chmod(0o600)


def _summary() -> dict:
    diag = {"dominantBottleneck": "cpu", "leakSuspected": False, "postGcSlopeBytesPerMinuteMedian": 1.0, "loadReachedTarget": True, "peakConnectedClientsMedian": 2, "processDurationSecondsMedian": 60}
    return {"schemaVersion": 1, "baselinePreset": "current", "candidatePreset": "balanced-g1", "overallVerdict": "inconclusive", "metrics": [{"name": "tick.p95Nanos", "baselineMedian": 1.0, "candidateMedian": 1.0, "delta": 0.0, "deltaPercent": 0.0, "ciLow": -1.0, "ciHigh": 1.0, "verdict": "inconclusive"}], "diagnostics": {"baseline": diag, "candidate": diag}}


def _input(path: Path, **changes: object) -> None:
    value = {"schemaVersion": 1, "campaignId": "campaign-20260819", "profileId": "minecraft-sunlit-cobblemon", "baselinePreset": "current", "candidatePreset": "balanced-g1", "createdAt": "2026-08-10T12:00:00Z", "finishedAt": "2026-08-10T13:00:00Z", "overallVerdict": "inconclusive", "summary": _summary()}
    value.update(changes); path.write_text(json.dumps(value), encoding="utf-8"); path.chmod(0o600)


def test_import_is_bounded_and_idempotent(tmp_path: Path):
    database, source = tmp_path / "state.db", tmp_path / "campaign.json"; _db(database); _input(source)
    assert module.import_campaign(source, database)["status"] == "imported"
    assert module.import_campaign(source, database)["status"] == "already_present"
    row = sqlite3.connect(database).execute("SELECT id,artifact_path,error_code FROM benchmark_runs").fetchone()
    assert row[0] == "swagbench-import-campaign-20260819" and row[1:] == (None, None)


def test_changed_campaign_with_same_id_refuses(tmp_path: Path):
    database, source = tmp_path / "state.db", tmp_path / "campaign.json"; _db(database); _input(source); module.import_campaign(source, database)
    changed = _summary(); changed["overallVerdict"] = "better"; _input(source, overallVerdict="better", summary=changed)
    with pytest.raises(module.ImportError, match="different data"): module.import_campaign(source, database)


def test_fractional_timestamp_order_is_checked_as_datetime(tmp_path: Path):
    database, source = tmp_path / "state.db", tmp_path / "campaign.json"; _db(database)
    _input(source, createdAt="2026-08-10T12:00:00.900Z", finishedAt="2026-08-10T12:00:00Z")
    with pytest.raises(module.ImportError, match="precedes"): module.import_campaign(source, database)


@pytest.mark.parametrize("change", [{"summary": {"schemaVersion": 1}}, {"artifactPath": "/etc/shadow"}, {"profileId": "minecraft"}])
def test_invalid_or_extra_data_fails_closed(tmp_path: Path, change: dict):
    database, source = tmp_path / "state.db", tmp_path / "campaign.json"; _db(database); _input(source, **change)
    with pytest.raises(module.ImportError): module.import_campaign(source, database)
