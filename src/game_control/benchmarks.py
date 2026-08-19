"""Root-owned SwagBench plans, execution, artifact validation, and history."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import SafeError
from .models import ProfileId
from .protocol import (
    BenchmarkMetricView,
    BenchmarkDiagnosticsView,
    BenchmarkOverview,
    BenchmarkPresetView,
    BenchmarkRunSummary,
)


_PRESET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_SUMMARY_BYTES = 2 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _connection(database: Any) -> sqlite3.Connection | Any:
    connection = getattr(database, "connection", database)
    if not hasattr(connection, "execute"):
        raise SafeError("state_unavailable", "benchmark state is unavailable")
    return connection


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkPreset(_ConfigModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    label: str = Field(min_length=1, max_length=64)


class BenchmarkPlan(_ConfigModel):
    profile: ProfileId
    driver: Path
    config: Path
    report_root: Path
    presets: tuple[BenchmarkPreset, ...] = Field(min_length=2, max_length=16)
    timeout_seconds: int = Field(default=21_600, ge=60, le=86_400)

    @field_validator("driver", "config", "report_root")
    @classmethod
    def normalized_absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("benchmark paths must be normalized and absolute")
        return value

    @model_validator(mode="after")
    def unique_presets_and_paths(self):
        if len({preset.id for preset in self.presets}) != len(self.presets):
            raise ValueError("benchmark preset IDs must be unique")
        if self.report_root in {self.driver, self.config}:
            raise ValueError("benchmark artifact root must be separate from executable inputs")
        return self


def parse_benchmark_plans(raw: Any) -> dict[ProfileId, BenchmarkPlan]:
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > 16:
        raise ValueError("benchmark configuration must be a bounded array")
    plans: dict[ProfileId, BenchmarkPlan] = {}
    for value in raw:
        plan = BenchmarkPlan.model_validate(value)
        if plan.profile in plans:
            raise ValueError("duplicate benchmark profile")
        plans[plan.profile] = plan
    return plans


def _secure_input(path: Path, *, executable: bool = False) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SafeError("benchmark_unavailable", "benchmark executable input is unavailable")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SafeError("benchmark_unavailable", "benchmark executable input is not root-controlled")
    if executable and not os.access(path, os.X_OK):
        raise SafeError("benchmark_unavailable", "benchmark driver is not executable")
    return path


def _artifact_path(root: Path, value: str) -> Path:
    if not value or len(value) > 4096 or "\n" in value or "\x00" in value:
        raise SafeError("benchmark_failed", "benchmark driver returned an invalid artifact")
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise SafeError("benchmark_failed", "benchmark driver returned an invalid artifact")
    try:
        candidate.relative_to(root)
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SafeError("benchmark_failed", "benchmark artifact escaped its approved root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise SafeError("benchmark_failed", "benchmark summary artifact is unavailable")
    info = candidate.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > _MAX_SUMMARY_BYTES:
        raise SafeError("benchmark_failed", "benchmark summary artifact is not trustworthy")
    return candidate


def _summary_models(
    raw: Any,
) -> tuple[str, tuple[BenchmarkMetricView, ...], BenchmarkDiagnosticsView, BenchmarkDiagnosticsView]:
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        raise SafeError("benchmark_failed", "benchmark summary schema is invalid")
    verdict = raw.get("overallVerdict")
    if verdict not in {"better", "worse", "mixed", "inconclusive"}:
        raise SafeError("benchmark_failed", "benchmark verdict is invalid")
    values = raw.get("metrics")
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise SafeError("benchmark_failed", "benchmark metrics are invalid")
    metrics: list[BenchmarkMetricView] = []
    for value in values:
        try:
            metric = BenchmarkMetricView.model_validate(value)
        except Exception as exc:
            raise SafeError("benchmark_failed", "benchmark metric is invalid") from exc
        metrics.append(metric)
    diagnostics = raw.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise SafeError("benchmark_failed", "benchmark diagnostics are invalid")
    try:
        baseline_diagnostics = BenchmarkDiagnosticsView.model_validate(diagnostics.get("baseline"))
        candidate_diagnostics = BenchmarkDiagnosticsView.model_validate(diagnostics.get("candidate"))
    except Exception as exc:
        raise SafeError("benchmark_failed", "benchmark diagnostics are invalid") from exc
    return verdict, tuple(metrics), baseline_diagnostics, candidate_diagnostics


class BenchmarkService:
    def __init__(
        self,
        plans: Mapping[ProfileId, BenchmarkPlan],
        *,
        database: Any,
        adapters: Mapping[Any, Any],
        profiles: Mapping[str, Any],
        slot_inspector: Any,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.plans = dict(plans)
        self.database = database
        self.adapters = adapters
        self.profiles = profiles
        self.slot_inspector = slot_inspector
        self.runner = runner
        self.clock = clock

    def _plan(self, profile_id: ProfileId) -> BenchmarkPlan:
        try:
            return self.plans[profile_id]
        except KeyError as exc:
            raise SafeError("benchmark_unavailable", "benchmarking is not configured for this profile") from exc

    async def prove_idle(self, profile_id: ProfileId) -> None:
        plan = self._plan(profile_id)
        del plan
        slot = self.slot_inspector.observe()
        if getattr(slot, "inconsistent", True) or getattr(slot, "owner", None) is not None:
            raise SafeError("invalid_state", "the shared game slot must be consistently idle before benchmarking")
        profile = self.profiles.get(profile_id.value)
        adapter = self.adapters.get(profile_id) or self.adapters.get(profile_id.value)
        if profile is None or adapter is None or not hasattr(adapter, "observe"):
            raise SafeError("benchmark_unavailable", "profile state could not be proven")
        observed = adapter.observe(profile)
        if hasattr(observed, "__await__"):
            observed = await observed
        if bool(getattr(observed, "running", False)):
            raise SafeError("invalid_state", "profile must be stopped before benchmarking")

    def prepare(self, action: Any, job_id: str) -> None:
        plan = self._plan(action.profile_id)
        allowed = {preset.id for preset in plan.presets}
        if action.baseline_preset not in allowed or action.candidate_preset not in allowed:
            raise SafeError("invalid_request", "benchmark preset is not configured")
        if action.baseline_preset == action.candidate_preset:
            raise SafeError("invalid_request", "baseline and candidate presets must differ")
        connection = _connection(self.database)
        connection.execute(
            "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                job_id,
                action.profile_id.value,
                action.baseline_preset,
                action.candidate_preset,
                "running",
                _iso(self.clock()),
            ),
        )
        connection.commit()

    def _execute(
        self, action: Any
    ) -> tuple[
        Path,
        dict[str, Any],
        str,
        tuple[BenchmarkMetricView, ...],
        BenchmarkDiagnosticsView,
        BenchmarkDiagnosticsView,
    ]:
        plan = self._plan(action.profile_id)
        driver = _secure_input(plan.driver, executable=True)
        config = _secure_input(plan.config)
        if plan.report_root.is_symlink() or not plan.report_root.is_dir():
            raise SafeError("benchmark_unavailable", "benchmark artifact root is unavailable")
        root_info = plan.report_root.stat()
        if root_info.st_uid != 0 or root_info.st_mode & 0o022:
            raise SafeError("benchmark_unavailable", "benchmark artifact root is not root-controlled")
        completed = self.runner(
            [
                str(driver),
                "--config",
                str(config),
                "--baseline",
                action.baseline_preset,
                "--candidate",
                action.candidate_preset,
            ],
            cwd="/",
            env={"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=plan.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise SafeError("benchmark_failed", "benchmark driver failed")
        lines = completed.stdout.splitlines()
        if len(lines) != 1:
            raise SafeError("benchmark_failed", "benchmark driver returned an invalid artifact")
        artifact = _artifact_path(plan.report_root, lines[0])
        try:
            raw = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SafeError("benchmark_failed", "benchmark summary could not be read") from exc
        verdict, metrics, baseline_diagnostics, candidate_diagnostics = _summary_models(raw)
        if raw.get("baselinePreset") != action.baseline_preset or raw.get("candidatePreset") != action.candidate_preset:
            raise SafeError("benchmark_failed", "benchmark summary presets do not match the request")
        return artifact, raw, verdict, metrics, baseline_diagnostics, candidate_diagnostics

    async def run(self, action: Any, job_id: str) -> BenchmarkRunSummary:
        try:
            artifact, raw, verdict, metrics, baseline_diagnostics, candidate_diagnostics = await asyncio.to_thread(
                self._execute, action
            )
        except Exception:
            self.fail(job_id, "benchmark_failed")
            raise
        plan = self._plan(action.profile_id)
        relative = str(artifact.relative_to(plan.report_root))
        safe_summary = json.dumps(
            {
                "metrics": [metric.model_dump(mode="json", by_alias=True) for metric in metrics],
                "baseline_diagnostics": baseline_diagnostics.model_dump(mode="json", by_alias=True),
                "candidate_diagnostics": candidate_diagnostics.model_dump(mode="json", by_alias=True),
            },
            separators=(",", ":"),
        )
        finished = _iso(self.clock())
        connection = _connection(self.database)
        connection.execute(
            "UPDATE benchmark_runs SET state='succeeded',finished_at=?,overall_verdict=?,"
            "summary_json=?,artifact_path=?,error_code=NULL WHERE id=? AND state='running'",
            (finished, verdict, safe_summary, relative, job_id),
        )
        connection.commit()
        return self.get_run(job_id)

    def fail(self, job_id: str, error_code: str) -> None:
        connection = _connection(self.database)
        connection.execute(
            "UPDATE benchmark_runs SET state='failed',finished_at=?,error_code=? "
            "WHERE id=? AND state='running'",
            (_iso(self.clock()), error_code[:64], job_id),
        )
        connection.commit()

    def get_run(self, job_id: str) -> BenchmarkRunSummary:
        connection = _connection(self.database)
        row = connection.execute(
            "SELECT id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,"
            "overall_verdict,summary_json,error_code FROM benchmark_runs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise SafeError("benchmark_unavailable", "benchmark run was not found")
        metrics: tuple[BenchmarkMetricView, ...] = ()
        baseline_diagnostics = None
        candidate_diagnostics = None
        if row[8]:
            try:
                summary = json.loads(row[8])
                metrics = tuple(BenchmarkMetricView.model_validate(item) for item in summary["metrics"])
                baseline_diagnostics = BenchmarkDiagnosticsView.model_validate(summary["baseline_diagnostics"])
                candidate_diagnostics = BenchmarkDiagnosticsView.model_validate(summary["candidate_diagnostics"])
            except Exception as exc:
                raise SafeError("state_unavailable", "benchmark history is invalid") from exc
        return BenchmarkRunSummary(
            id=row[0],
            profile_id=ProfileId(row[1]),
            baseline_preset=row[2],
            candidate_preset=row[3],
            state=row[4],
            created_at=row[5],
            finished_at=row[6],
            overall_verdict=row[7],
            metrics=metrics,
            baseline_diagnostics=baseline_diagnostics,
            candidate_diagnostics=candidate_diagnostics,
            error_code=row[9],
        )

    async def overview(self, action: Any, actor: str | None = None, request_id: Any = None) -> BenchmarkOverview:
        plan = self.plans.get(action.profile_id)
        if plan is None:
            return BenchmarkOverview(profile_id=action.profile_id, available=False, presets=(), runs=())
        connection = _connection(self.database)
        rows = connection.execute(
            "SELECT id FROM benchmark_runs WHERE profile_id=? ORDER BY created_at DESC,id DESC LIMIT 20",
            (action.profile_id.value,),
        ).fetchall()
        return BenchmarkOverview(
            profile_id=action.profile_id,
            available=True,
            presets=tuple(BenchmarkPresetView(id=preset.id, label=preset.label) for preset in plan.presets),
            runs=tuple(self.get_run(row[0]) for row in rows),
        )


__all__ = ["BenchmarkPlan", "BenchmarkPreset", "BenchmarkService", "parse_benchmark_plans"]
