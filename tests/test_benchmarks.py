from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control.benchmarks import BenchmarkService, parse_benchmark_plans
from game_control.errors import SafeError
from game_control.models import ProfileId
from game_control.protocol import GetBenchmarks, RunBenchmark


def _database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE benchmark_runs("
        "id TEXT PRIMARY KEY,profile_id TEXT,baseline_preset TEXT,candidate_preset TEXT,"
        "state TEXT,created_at TEXT,finished_at TEXT,overall_verdict TEXT,summary_json TEXT,"
        "artifact_path TEXT,error_code TEXT)"
    )
    return connection


def _plan(tmp_path: Path):
    driver = tmp_path / "swagbench-ab"
    driver.write_text("#!/bin/sh\nexit 0\n")
    driver.chmod(0o700)
    config = tmp_path / "swagbench.json"
    config.write_text("{}")
    reports = tmp_path / "reports"
    reports.mkdir()
    return parse_benchmark_plans(
        [
            {
                "profile": "minecraft-sunlit-cobblemon",
                "driver": str(driver),
                "config": str(config),
                "report_root": str(reports),
                "presets": [
                    {"id": "current", "label": "Current production"},
                    {"id": "candidate", "label": "Candidate tuning"},
                ],
            }
        ]
    ), reports


def _summary(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "baselinePreset": "current",
                "candidatePreset": "candidate",
                "overallVerdict": "better",
                "metrics": [
                    {
                        "name": "tick.p95Nanos",
                        "baselineMedian": 20_000_000,
                        "candidateMedian": 15_000_000,
                        "delta": -5_000_000,
                        "deltaPercent": -0.25,
                        "ciLow": -6_000_000,
                        "ciHigh": -4_000_000,
                        "verdict": "better",
                    }
                ],
                "diagnostics": {
                    "baseline": {
                        "dominantBottleneck": "cpu",
                        "leakSuspected": False,
                        "postGcSlopeBytesPerMinuteMedian": 1024,
                        "loadReachedTarget": True,
                        "peakConnectedClientsMedian": 2,
                        "processDurationSecondsMedian": 120,
                    },
                    "candidate": {
                        "dominantBottleneck": "cpu",
                        "leakSuspected": False,
                        "postGcSlopeBytesPerMinuteMedian": 512,
                        "loadReachedTarget": True,
                        "peakConnectedClientsMedian": 2,
                        "processDurationSecondsMedian": 115,
                    },
                },
            }
        )
    )
    path.chmod(0o600)


def _service(tmp_path: Path, runner):
    plans, reports = _plan(tmp_path)
    profile_id = ProfileId.MINECRAFT_SUNLIT_COBBLEMON

    class Adapter:
        async def observe(self, _profile):
            return SimpleNamespace(running=False)

    service = BenchmarkService(
        plans,
        database=_database(),
        adapters={profile_id: Adapter()},
        profiles={profile_id.value: SimpleNamespace(id=profile_id)},
        slot_inspector=SimpleNamespace(observe=lambda: SimpleNamespace(owner=None, inconsistent=False)),
        runner=runner,
    )
    return service, reports


@pytest.mark.asyncio
async def test_benchmark_service_runs_only_configured_presets_and_persists_safe_summary(tmp_path: Path):
    captured = []

    def runner(command, **kwargs):
        captured.append((command, kwargs))
        summary = tmp_path / "reports" / "run-1" / "summary.json"
        summary.parent.mkdir()
        _summary(summary)
        return subprocess.CompletedProcess(command, 0, stdout=f"{summary}\n", stderr="")

    service, _reports = _service(tmp_path, runner)
    action = RunBenchmark(
        kind="run_benchmark",
        profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
        baseline_preset="current",
        candidate_preset="candidate",
    )
    await service.prove_idle(action.profile_id)
    service.prepare(action, "job1")
    result = await service.run(action, "job1")
    assert result.state == "succeeded"
    assert result.overall_verdict == "better"
    assert result.metrics[0].candidate_median == 15_000_000
    assert result.candidate_diagnostics.load_reached_target is True
    assert captured[0][0][-4:] == ["--baseline", "current", "--candidate", "candidate"]
    assert captured[0][1]["check"] is False
    overview = await service.overview(
        GetBenchmarks(kind="get_benchmarks", profile_id=action.profile_id)
    )
    assert overview.available is True
    assert [preset.id for preset in overview.presets] == ["current", "candidate"]
    assert overview.runs[0].id == "job1"


@pytest.mark.asyncio
async def test_benchmark_service_fails_closed_when_slot_is_owned(tmp_path: Path):
    service, _reports = _service(tmp_path, lambda *_args, **_kwargs: None)
    service.slot_inspector = SimpleNamespace(
        observe=lambda: SimpleNamespace(owner=ProfileId.MINECRAFT, inconsistent=False)
    )
    with pytest.raises(SafeError, match="consistently idle") as error:
        await service.prove_idle(ProfileId.MINECRAFT_SUNLIT_COBBLEMON)
    assert error.value.code == "invalid_state"


@pytest.mark.asyncio
async def test_benchmark_service_rejects_artifact_outside_approved_root(tmp_path: Path):
    outside = tmp_path / "outside.json"
    _summary(outside)

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=f"{outside}\n", stderr="")

    service, _reports = _service(tmp_path, runner)
    action = RunBenchmark(
        kind="run_benchmark",
        profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
        baseline_preset="current",
        candidate_preset="candidate",
    )
    service.prepare(action, "job2")
    with pytest.raises(SafeError, match="escaped"):
        await service.run(action, "job2")
    assert service.get_run("job2").state == "failed"


def test_benchmark_plan_rejects_duplicate_presets_and_raw_unknown_fields(tmp_path: Path):
    plans, reports = _plan(tmp_path)
    assert plans[ProfileId.MINECRAFT_SUNLIT_COBBLEMON].report_root == reports
    with pytest.raises(ValueError):
        parse_benchmark_plans(
            [
                {
                    "profile": "minecraft-sunlit-cobblemon",
                    "driver": "/usr/local/libexec/swagbench-ab",
                    "config": "/etc/game-control/swagbench.json",
                    "report_root": "/var/lib/game-control/benchmarks",
                    "presets": [{"id": "same", "label": "A"}, {"id": "same", "label": "B"}],
                    "raw_jvm_args": ["-Xmx2G"],
                }
            ]
        )
