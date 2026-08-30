from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from game_control.benchmarks import BenchmarkPreset
from game_control.driver_preflight import DriverPreflight, _preset_digest, _sha256, _validate_frozen_provenance
from game_control.errors import SafeError


def _payload() -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "driverSha256": "d" * 64,
        "configSha256": "c" * 64,
        "presetDigests": {"current": "1" * 64, "candidate": "2" * 64},
        "presetArgvDigests": {"current": "a" * 64, "candidate": "b" * 64},
        "pairPlan": {"minimumCompletePairs": 1, "maximumPairs": 1},
        "primaryEndpoints": [{"name": "tick.p95Nanos", "effectThreshold": 0.1}],
        "thresholds": {"tick.p95Nanos": 0.1},
        "statisticsImplementation": {"name": "fixture", "fixtures": "fixture-v1"},
    }


def test_driver_preflight_runs_check_and_freezes_without_database(tmp_path: Path):
    driver = tmp_path / "driver"
    driver.write_text("#!/bin/sh\nexit 0\n")
    driver.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text("{}")
    plan = SimpleNamespace(
        driver=driver,
        config=config,
        timeout_seconds=60,
        presets=(
            BenchmarkPreset(id="current", label="Current"),
            BenchmarkPreset(id="candidate", label="Candidate"),
        ),
    )
    action = SimpleNamespace(baseline_preset="current", candidate_preset="candidate")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        payload = _payload()
        payload["driverSha256"] = _sha256(driver)
        payload["configSha256"] = _sha256(config)
        payload["presetDigests"] = {
            "current": _preset_digest(plan.presets[0]),
            "candidate": _preset_digest(plan.presets[1]),
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    preflight = DriverPreflight()
    raw = preflight.run(plan, "job-1", runner=runner)
    frozen = preflight.freeze(action, plan, raw)

    assert calls[0][0][-1] == "--check"
    assert calls[0][1]["check"] is False
    assert frozen["preflight"] == raw
    assert set(frozen) == {"version", "driverSha256", "configSha256", "presetDigests", "presetArgvDigests", "preflight"}


def test_driver_preflight_rejects_frozen_provenance_before_caller_insert():
    action = SimpleNamespace(baseline_preset="current", candidate_preset="candidate")
    frozen = {
        "version": 2,
        "driverSha256": "d" * 64,
        "configSha256": "c" * 64,
        "presetDigests": {"current": "1" * 64, "candidate": "2" * 64},
        "presetArgvDigests": {"current": "a" * 64, "candidate": "b" * 64},
        "preflight": _payload(),
    }
    frozen["preflight"] = dict(frozen["preflight"], thresholds={"tick.p95Nanos": float("nan")})

    with pytest.raises(SafeError, match="threshold"):
        _validate_frozen_provenance(action, frozen)
