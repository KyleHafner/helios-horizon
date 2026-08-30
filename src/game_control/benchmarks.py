"""Root-owned SwagBench plans, execution, artifact validation, and history."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import hashlib
import os
import threading
import inspect
import csv
import io
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import SafeError
from .interim_maintenance_control import maintenance_popen
from .models import ProfileId
from .protocol import (
    BenchmarkMetricView,
    BenchmarkDiagnosticsView,
    BenchmarkOverview,
    BenchmarkPresetView,
    BenchmarkRunSummary,
    BenchmarkTrendPoint,
    BenchmarkTrendMetric,
)


_PRESET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_SUMMARY_BYTES = 2 * 1024 * 1024
_MAX_PROCESS_OUTPUT_BYTES = _MAX_SUMMARY_BYTES
_PROCESS_READ_CHUNK = 64 * 1024
_FAILURE_CATEGORIES = frozenset({"none", "driver", "timeout", "incomplete", "degraded", "cancelled", "cleanup"})
_STAGES = frozenset({"prepared", "executing", "validating", "completed", "failed", "interrupted"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _connection(database: Any) -> sqlite3.Connection | Any:
    connection = getattr(database, "connection", database)
    if not hasattr(connection, "execute"):
        raise SafeError("state_unavailable", "benchmark state is unavailable")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _preset_digest(preset: BenchmarkPreset) -> str:
    payload = json.dumps({"id": preset.id, "label": preset.label}, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _run_process_group(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    registry = kwargs.pop("process_registry", None)
    process_key = kwargs.pop("process_key", None)
    registry_lock = kwargs.pop("process_lock", None)
    # ``capture_output`` belongs to subprocess.run(), while the maintenance
    # wrapper deliberately exposes a Popen-compatible boundary so it can keep
    # the process in maintenance.slice and register it for cancellation.
    # Translate the run-style option here instead of forwarding it to Popen.
    if kwargs.pop("capture_output", False):
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = maintenance_popen(command, start_new_session=True, **kwargs)
    if registry is not None and process_key is not None:
        if registry_lock is None:
            registry[process_key] = process
        else:
            with registry_lock:
                registry[process_key] = process
    wrapped_command = process.args

    def cleanup_registry() -> None:
        if registry is not None and process_key is not None:
            if registry_lock is None:
                registry.pop(process_key, None)
            else:
                with registry_lock:
                    registry.pop(process_key, None)

    # Popen.communicate() drains both pipes concurrently, but retains all
    # output.  Benchmark drivers are untrusted inputs, so drain each stream in
    # bounded reader threads and terminate the maintenance process group as
    # soon as either stream exceeds the cap.
    streams = (getattr(process, "stdout", None), getattr(process, "stderr", None))
    if all(stream is not None for stream in streams):
        output: list[Any] = [[], []]
        overflow = threading.Event()

        def drain(index: int, stream: Any) -> None:
            size = 0
            while not overflow.is_set():
                chunk = stream.read(_PROCESS_READ_CHUNK)
                if not chunk:
                    return
                chunk_size = len(chunk.encode() if isinstance(chunk, str) else chunk)
                size += chunk_size
                if size > _MAX_PROCESS_OUTPUT_BYTES:
                    overflow.set()
                    return
                output[index].append(chunk)

        readers = [threading.Thread(target=drain, args=(index, stream), daemon=True)
                   for index, stream in enumerate(streams)]
        for reader in readers:
            reader.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        timed_out = False
        try:
            while process.poll() is None:
                if overflow.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.01)
            if overflow.is_set() or timed_out:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired, RuntimeError):
                    process.kill()
                    process.wait()
            for reader in readers:
                reader.join(timeout=2)
        finally:
            for reader in readers:
                if reader.is_alive():
                    overflow.set()
        if overflow.is_set() and not timed_out:
            cleanup_registry()
            raise SafeError("benchmark_failed", "benchmark driver output exceeded bounded limit")
        if timed_out:
            cleanup_registry()
            raise subprocess.TimeoutExpired(wrapped_command, timeout)
        stdout = "".join(output[0]) if output[0] and isinstance(output[0][0], str) else ""
        stderr = "".join(output[1]) if output[1] and isinstance(output[1][0], str) else ""
        if output[0] and not isinstance(output[0][0], str):
            stdout = b"".join(output[0])
        if output[1] and not isinstance(output[1][0], str):
            stderr = b"".join(output[1])
    else:
        # Lightweight injected runners used by older callers/tests do not
        # expose Popen streams; retain their communicate contract.
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired, RuntimeError):
                process.kill()
                process.wait()
            cleanup_registry()
            raise subprocess.TimeoutExpired(wrapped_command, timeout, output=exc.output, stderr=exc.stderr)
    cleanup_registry()
    return subprocess.CompletedProcess(wrapped_command, process.returncode, stdout, stderr)


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
    if not isinstance(raw, dict) or raw.get("schemaVersion") not in {1, 2}:
        raise SafeError("benchmark_failed", "benchmark summary schema is invalid")
    if raw.get("schemaVersion") == 2:
        provenance_keys = ("driverSha256", "configSha256", "presetDigests", "presetArgvDigests", "statisticsImplementation")
        if any(key not in raw for key in provenance_keys):
            raise SafeError("benchmark_failed", "benchmark v2 provenance is invalid")
        if not isinstance(raw["driverSha256"], str) or not isinstance(raw["configSha256"], str):
            raise SafeError("benchmark_failed", "benchmark v2 provenance is invalid")
        if not isinstance(raw["presetDigests"], dict) or not isinstance(raw["presetArgvDigests"], dict):
            raise SafeError("benchmark_failed", "benchmark v2 provenance is invalid")
        implementation = raw["statisticsImplementation"]
        if not isinstance(implementation, dict) or not isinstance(implementation.get("fixtures"), str):
            raise SafeError("benchmark_failed", "benchmark v2 statistics provenance is invalid")
        pair_plan = raw.get("pairPlan")
        pairs = raw.get("pairs")
        if (
            not isinstance(pair_plan, dict)
            or not isinstance(pair_plan.get("minimumCompletePairs"), int)
            or not isinstance(pair_plan.get("maximumPairs"), int)
            or not isinstance(pairs, list)
            or len(pairs) > 64
        ):
            raise SafeError("benchmark_failed", "benchmark v2 pair contract is invalid")
        if any(
            not isinstance(pair, dict)
            or not isinstance(pair.get("complete"), bool)
            or not isinstance(pair.get("degraded"), bool)
            for pair in pairs
        ):
            raise SafeError("benchmark_failed", "benchmark v2 pair contract is invalid")
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


def _pair_counts(raw: Mapping[str, Any]) -> tuple[int, int, bool]:
    """Derive completion from the driver's canonical pairPlan+pairs contract."""
    if raw.get("schemaVersion") != 2:
        planned = raw.get("plannedPairs", 2)
        completed = raw.get("completedPairs", planned)
        return (planned if isinstance(planned, int) else 0,
                completed if isinstance(completed, int) else 0, False)
    plan = raw["pairPlan"]
    pairs = raw["pairs"]
    planned = len(pairs)
    completed = sum(1 for pair in pairs if pair["complete"] and not pair["degraded"])
    minimum = plan["minimumCompletePairs"]
    maximum = plan["maximumPairs"]
    if planned > maximum or completed > planned:
        raise SafeError("benchmark_failed", "benchmark v2 pair counts are invalid")
    return planned, completed, completed < minimum


class BenchmarkService:
    def __init__(
        self,
        plans: Mapping[ProfileId, BenchmarkPlan],
        *,
        database: Any,
        adapters: Mapping[Any, Any],
        profiles: Mapping[str, Any],
        slot_inspector: Any,
        runner: Callable[..., subprocess.CompletedProcess[str]] = _run_process_group,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.plans = dict(plans)
        self.database = database
        self.adapters = adapters
        self.profiles = profiles
        self.slot_inspector = slot_inspector
        self.runner = runner
        self.clock = clock
        self._active_processes: dict[str, Any] = {}
        self._process_lock = threading.Lock()
        self._reconcile_startup()

    def _reconcile_startup(self) -> None:
        """Close rows left running by a controller/process restart."""
        try:
            connection = _connection(self.database)
        except SafeError:
            # Wiring-only/test seams may intentionally provide no database;
            # the first real controller with a canonical DB performs cleanup.
            return
        self._require_schema(connection)
        connection.execute(
            "UPDATE benchmark_runs SET state='failed', finished_at=?, error_code='interrupted', failure_category='driver', stage='interrupted', progress=0 WHERE state='running'",
            (_iso(self.clock()),),
        )
        connection.commit()

    def _runner_options(self, job_id: str) -> dict[str, Any]:
        try:
            parameters = inspect.signature(self.runner).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "process_registry" not in parameters and not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            return {}
        return {"process_registry": self._active_processes, "process_key": job_id, "process_lock": self._process_lock}

    def _run_preflight(self, plan: BenchmarkPlan, job_id: str) -> dict[str, Any]:
        driver = _secure_input(plan.driver, executable=True)
        config = _secure_input(plan.config)
        result = self.runner(
            [str(driver), "--config", str(config), "--check"], cwd="/",
            env={"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=min(plan.timeout_seconds, 300), check=False,
            **self._runner_options(job_id),
        )
        if result.returncode != 0:
            raise SafeError("benchmark_failed", "benchmark driver preflight failed")
        try:
            decoded = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SafeError("benchmark_failed", "benchmark preflight provenance is invalid") from exc
        required = ("schemaVersion", "driverSha256", "configSha256", "presetDigests", "presetArgvDigests", "pairPlan", "primaryEndpoints", "thresholds", "statisticsImplementation")
        if not isinstance(decoded, dict) or decoded.get("schemaVersion") != 2 or any(key not in decoded for key in required):
            raise SafeError("benchmark_failed", "benchmark preflight provenance is incomplete")
        return decoded

    def cancel(self, job_id: str) -> bool:
        with self._process_lock:
            process = self._active_processes.get(job_id)
        if process is None or process.poll() is not None:
            return False
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        return True

    def _plan(self, profile_id: ProfileId) -> BenchmarkPlan:
        try:
            return self.plans[profile_id]
        except KeyError as exc:
            raise SafeError("benchmark_unavailable", "benchmarking is not configured for this profile") from exc

    @staticmethod
    def _require_schema(connection: Any) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(benchmark_runs)")}
        required = {
            "provenance_json", "planned_pairs", "completed_pairs",
            "primary_endpoints_json", "thresholds_json", "driver_verdict",
            "failure_category", "stage", "progress", "artifact_sha256",
        }
        if not required.issubset(columns):
            raise SafeError("state_unavailable", "benchmark state database requires offline schema migration")

    def _stage(self, job_id: str, stage: str, progress: int) -> None:
        if stage not in _STAGES:
            raise ValueError("invalid benchmark stage")
        connection = _connection(self.database)
        self._require_schema(connection)
        connection.execute("UPDATE benchmark_runs SET stage=?,progress=? WHERE id=? AND state='running'", (stage, max(0, min(100, int(progress))), job_id))
        connection.commit()

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
        self._require_schema(connection)
        preflight: dict[str, Any] | None = None
        preflight = self._run_preflight(plan, job_id)
        provenance = {
            "version": 2,
            "driverSha256": _sha256(plan.driver),
            "configSha256": _sha256(plan.config),
            "presetDigests": {
                preset.id: _preset_digest(preset)
                for preset in plan.presets
                if preset.id in {action.baseline_preset, action.candidate_preset}
            },
        }
        if preflight is not None:
            provenance["preflight"] = preflight
        connection.execute(
            "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at,provenance_json,planned_pairs,completed_pairs,failure_category) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                action.profile_id.value,
                action.baseline_preset,
                action.candidate_preset,
                "running",
                _iso(self.clock()),
                json.dumps(provenance, separators=(",", ":"), sort_keys=True),
                "2",
                "0",
                "none",
            ),
        )
        connection.commit()
        connection.execute("UPDATE benchmark_runs SET stage='prepared',progress=0 WHERE id=?", (job_id,))
        connection.commit()

    def _execute(
        self, action: Any, job_id: str
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
        preflight = self._run_preflight(plan, job_id)
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
            **self._runner_options(job_id),
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
        if preflight is not None:
            stored_row = _connection(self.database).execute(
                "SELECT provenance_json FROM benchmark_runs WHERE id=?", (job_id,)
            ).fetchone()
            try:
                stored = json.loads(stored_row[0])["preflight"] if stored_row and stored_row[0] else None
            except (TypeError, KeyError, json.JSONDecodeError) as exc:
                raise SafeError("benchmark_failed", "frozen benchmark preflight is unavailable") from exc
            if stored != preflight:
                raise SafeError("benchmark_failed", "benchmark preflight changed after preparation")
            for key in ("driverSha256", "configSha256", "presetDigests", "presetArgvDigests", "pairPlan", "primaryEndpoints", "thresholds", "statisticsImplementation"):
                if raw.get(key) != preflight.get(key):
                    raise SafeError("benchmark_failed", f"benchmark provenance mismatch: {key}")
        return artifact, raw, verdict, metrics, baseline_diagnostics, candidate_diagnostics

    async def run(self, action: Any, job_id: str) -> BenchmarkRunSummary:
        self._stage(job_id, "executing", 10)
        try:
            artifact, raw, verdict, metrics, baseline_diagnostics, candidate_diagnostics = await asyncio.to_thread(
                self._execute, action, job_id
            )
        except subprocess.TimeoutExpired:
            self.fail(job_id, "timeout")
            raise SafeError("benchmark_failed", "benchmark driver timed out")
        except asyncio.CancelledError:
            self.fail(job_id, "cancelled")
            raise
        except Exception:
            self.fail(job_id, "driver")
            raise
        self._stage(job_id, "validating", 80)
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
        self._require_schema(connection)
        failure_category = str(raw.get("failureCategory", "none"))
        if failure_category not in _FAILURE_CATEGORIES:
            failure_category = "degraded"
        planned_pairs, completed_pairs, incomplete = _pair_counts(raw)
        if planned_pairs < 1:
            raise SafeError("benchmark_failed", "benchmark pair plan is empty")
        if incomplete:
            verdict = "inconclusive"
            failure_category = "incomplete"
        endpoints = raw.get("primaryEndpoints", [])
        if raw.get("schemaVersion") == 2 and (not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 16 or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            for item in endpoints
        )):
            raise SafeError("benchmark_failed", "benchmark primary endpoint data is invalid")
        if not isinstance(endpoints, list):
            endpoints = []
        thresholds = raw.get("thresholds")
        if not isinstance(thresholds, dict):
            thresholds = {
                item["name"]: item.get("effectThreshold")
                for item in endpoints
                if item.get("effectThreshold") is not None
            }
        try:
            endpoints_json = json.dumps(endpoints, separators=(",", ":"), sort_keys=True, allow_nan=False)
            thresholds_json = json.dumps(thresholds, separators=(",", ":"), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SafeError("benchmark_failed", "benchmark endpoint data is not serializable") from exc
        if len(endpoints_json) > 4096 or len(thresholds_json) > 4096:
            raise SafeError("benchmark_failed", "benchmark endpoint data exceeds bounds")
        connection.execute(
            "UPDATE benchmark_runs SET state='succeeded',finished_at=?,overall_verdict=?,"
            "summary_json=?,artifact_path=?,artifact_sha256=?,error_code=NULL,completed_pairs=?,driver_verdict=?,failure_category=?,primary_endpoints_json=?,thresholds_json=?,stage='completed',progress=100 WHERE id=? AND state='running'",
            (finished, verdict, safe_summary, relative, _sha256(artifact), completed_pairs, str(raw.get("driverVerdict", verdict))[:32], failure_category, endpoints_json, thresholds_json, job_id),
        )
        connection.commit()
        return self.get_run(job_id)

    def fail(self, job_id: str, error_code: str) -> None:
        connection = _connection(self.database)
        self._require_schema(connection)
        connection.execute(
            "UPDATE benchmark_runs SET state='failed',finished_at=?,error_code=?, "
            "failure_category=?,stage=?,progress=? WHERE id=? AND state='running'",
            (_iso(self.clock()), error_code[:64], error_code if error_code in _FAILURE_CATEGORIES else "driver", "interrupted" if error_code == "interrupted" else "failed", 0, job_id),
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

    def history_page(self, profile_id: ProfileId, *, cursor: str | None = None, limit: int = 20) -> tuple[tuple[Any, ...], str | None]:
        """Fetch bounded benchmark history with one keyset query."""
        limit = max(1, min(int(limit), 100))
        connection = _connection(self.database)
        if cursor:
            rows = connection.execute(
                "SELECT id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json,error_code FROM benchmark_runs WHERE profile_id=? AND (created_at,id) < (SELECT created_at,id FROM benchmark_runs WHERE id=?) ORDER BY created_at DESC,id DESC LIMIT ?",
                (profile_id.value, cursor, limit + 1),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json,error_code FROM benchmark_runs WHERE profile_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (profile_id.value, limit + 1),
            ).fetchall()
        next_cursor = rows[limit - 1][0] if len(rows) > limit else None
        return tuple(rows[:limit]), next_cursor

    def export_history(self, profile_id: ProfileId, *, fmt: str = "json", limit: int = 100) -> str:
        rows, _ = self.history_page(profile_id, limit=min(max(1, int(limit)), 100))
        if fmt == "json":
            content = json.dumps([dict(zip(("id", "profile_id", "baseline_preset", "candidate_preset", "state", "created_at", "finished_at", "overall_verdict", "summary_json", "error_code"), row)) for row in rows], separators=(",", ":"))
            if len(content.encode()) > _MAX_SUMMARY_BYTES:
                raise SafeError("benchmark_failed", "benchmark export exceeds bounded size")
            return content
        if fmt != "csv":
            raise ValueError("format must be json or csv")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("id", "profile_id", "baseline_preset", "candidate_preset", "state", "created_at", "finished_at", "overall_verdict", "error_code"))
        for row in rows:
            values = tuple(row[:8]) + (row[9],)
            writer.writerow(("'" + str(value) if isinstance(value, str) and value[:1] in "=+-@" else value) for value in values)
        content = output.getvalue()
        if len(content.encode()) > _MAX_SUMMARY_BYTES:
            raise SafeError("benchmark_failed", "benchmark export exceeds bounded size")
        return content

    def trends(self, profile_id: ProfileId, *, limit: int = 20) -> tuple[dict[str, Any], ...]:
        """Return bounded, corruption-tolerant verdict trend points."""
        rows, _ = self.history_page(profile_id, limit=limit)
        points: list[dict[str, Any]] = []
        for row in rows:
            if row[7] is None:
                continue
            points.append({"id": row[0], "finishedAt": row[6], "verdict": row[7]})
        return tuple(points)

    def import_batch(self, records: list[Mapping[str, Any]], *, profile_id: ProfileId) -> int:
        """Import completed campaigns idempotently across profiles/preset pairs."""
        connection = _connection(self.database)
        self._require_schema(connection)
        connection.execute("SAVEPOINT benchmark_import")
        def invalid(message: str) -> None:
            connection.execute("ROLLBACK TO benchmark_import")
            connection.execute("RELEASE benchmark_import")
            raise SafeError("invalid_request", message)
        if not isinstance(records, list) or len(records) > 1000:
            invalid("benchmark import batch is invalid or too large")
        imported = 0
        seen: set[str] = set()
        now = _iso(self.clock())
        for record in records:
            if not isinstance(record, Mapping) or record.get("profileId") != profile_id.value:
                invalid("benchmark import profile mismatch")
            run_id = record.get("id")
            baseline = record.get("baselinePreset")
            candidate = record.get("candidatePreset")
            verdict = record.get("overallVerdict")
            if not isinstance(run_id, str) or not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", run_id) or run_id in seen:
                invalid("benchmark import id is invalid or duplicated")
            seen.add(run_id)
            if not isinstance(baseline, str) or not _PRESET_RE.fullmatch(baseline) or not isinstance(candidate, str) or not _PRESET_RE.fullmatch(candidate) or baseline == candidate:
                invalid("benchmark import presets are invalid")
            if verdict not in {"better", "worse", "mixed", "inconclusive"}:
                invalid("benchmark import verdict is invalid")
            created = record.get("createdAt", now)
            finished = record.get("finishedAt", now)
            for timestamp in (created, finished):
                if not isinstance(timestamp, str):
                    invalid("benchmark import timestamp is invalid")
                try:
                    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError as exc:
                    invalid("benchmark import timestamp is invalid")
            summary = record.get("summary", {})
            try:
                encoded_summary = json.dumps(summary, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                invalid("benchmark import summary is invalid")
            if len(encoded_summary.encode()) > _MAX_SUMMARY_BYTES:
                invalid("benchmark import summary is too large")
            artifact_path = record.get("artifactPath")
            artifact_sha = record.get("artifactSha256")
            if artifact_path is not None and (not isinstance(artifact_path, str) or Path(artifact_path).is_absolute() or ".." in Path(artifact_path).parts):
                invalid("benchmark import artifact path is invalid")
            if artifact_sha is not None and (not isinstance(artifact_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha)):
                invalid("benchmark import artifact digest is invalid")
            existing = connection.execute("SELECT profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json,artifact_path,artifact_sha256 FROM benchmark_runs WHERE id=?", (run_id,)).fetchone()
            canonical = (profile_id.value, baseline, candidate, "succeeded", created, finished, verdict, encoded_summary, artifact_path, artifact_sha)
            if existing is not None:
                if tuple(existing) != canonical:
                    invalid("benchmark import conflicts with an existing run")
                continue
            try:
                connection.execute(
                    "INSERT INTO benchmark_runs(id,profile_id,baseline_preset,candidate_preset,state,created_at,finished_at,overall_verdict,summary_json,artifact_path,artifact_sha256,stage,progress) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, *canonical, "completed", 100),
                )
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK TO benchmark_import")
                connection.execute("RELEASE benchmark_import")
                raise SafeError("state_unavailable", "benchmark import could not be committed") from exc
            imported += 1
        connection.execute("RELEASE benchmark_import")
        try:
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise SafeError("state_unavailable", "benchmark import could not be committed") from exc
        return imported

    def record_rolling_regression(
        self, *, profile_id: ProfileId, baseline: str, candidate: str,
        metric: str, campaign: str, value: float, threshold: float,
        evidence: Mapping[str, Any], rolling_k: int = 3,
        direction: str | None = None,
        notify: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> bool:
        """Persist one deduplicated rolling-K regression in campaign history.

        The campaign row is the canonical durable boundary; the embedded
        bounded record keeps this compatible with already-migrated v4 DBs.
        """
        connection = _connection(self.database)
        row = connection.execute(
            "SELECT id,summary_json FROM benchmark_runs WHERE profile_id=? AND baseline_preset=? AND candidate_preset=? AND state='succeeded' ORDER BY finished_at DESC LIMIT 1",
            (profile_id.value, baseline, candidate),
        ).fetchone()
        if row is None:
            return False
        try:
            summary = json.loads(row[1] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = {}
        records = summary.get("rollingRegressions", [])
        if not isinstance(records, list):
            records = []
        key = f"{profile_id.value}:{baseline}:{candidate}:{metric}:{campaign}"
        existing = next((item for item in records if isinstance(item, dict) and item.get("dedupKey") == key), None)
        if existing is not None and existing.get("notified") is True:
            return False
        comparable = connection.execute(
            "SELECT summary_json FROM benchmark_runs WHERE profile_id=? AND baseline_preset=? AND candidate_preset=? AND state='succeeded' AND overall_verdict IS NOT NULL ORDER BY finished_at DESC LIMIT ?",
            (profile_id.value, baseline, candidate, max(1, min(32, int(rolling_k)))),
        ).fetchall()
        observed = []
        for (raw,) in comparable:
            try:
                payload = json.loads(raw or "{}")
                for item in payload.get("metrics", ()):
                    if isinstance(item, dict) and item.get("name") == metric:
                        candidate_value = item.get("candidate_median", item.get("value"))
                        if isinstance(candidate_value, (int, float)):
                            observed.append(float(candidate_value))
                        break
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        required_k = max(1, min(32, int(rolling_k)))
        actual_k = len(observed)
        if actual_k < required_k or direction not in {"above", "below"}:
            return False
        rolling_value = sum(observed) / actual_k
        breached = rolling_value > threshold if direction == "above" else rolling_value < threshold
        if not breached:
            return False
        record = existing or {"dedupKey": key, "metric": metric, "campaign": campaign}
        record.update({"value": rolling_value, "threshold": threshold, "rollingK": actual_k, "evidence": dict(evidence), "notified": False})
        if existing is None:
            records.append(record)
        summary["rollingRegressions"] = records[-32:]
        connection.execute("UPDATE benchmark_runs SET summary_json=? WHERE id=?", (json.dumps(summary, separators=(",", ":")), row[0]))
        connection.commit()
        event = {"profileId": profile_id.value, "metric": metric, "campaign": campaign, "value": rolling_value, "threshold": threshold, "rollingK": actual_k, "evidence": dict(evidence)}
        if notify is not None:
            try:
                notify("benchmark_regression", event)
            except Exception:
                return True
            record["notified"] = True
            connection.execute("UPDATE benchmark_runs SET summary_json=? WHERE id=?", (json.dumps(summary, separators=(",", ":")), row[0]))
            connection.commit()
        return True

    def evaluate_completed_regression(self, profile_id: ProfileId, result: Any) -> bool:
        """Evaluate only against an explicitly declared policy.

        Driver verdicts are evidence, not alert policy. Until a campaign
        carries a bounded metric direction/threshold policy, fail closed and
        leave the alert state unchanged.
        """
        policy = getattr(result, "regression_policy", None)
        if not isinstance(policy, Mapping):
            return False
        breached = False
        for item in getattr(result, "metrics", ()):
            name = getattr(item, "name", None)
            spec = policy.get(name) if isinstance(name, str) else None
            if not isinstance(spec, Mapping):
                continue
            if self.record_rolling_regression(
                profile_id=profile_id,
                baseline=getattr(result, "baseline_preset", ""),
                candidate=getattr(result, "candidate_preset", ""),
                metric=name,
                campaign=getattr(result, "id", ""),
                value=float(getattr(item, "candidate_median", 0.0)),
                threshold=float(spec.get("threshold")),
                direction=spec.get("direction"),
                evidence={"source": "completed_campaign"},
                rolling_k=int(spec.get("rollingK", 3)),
            ):
                breached = True
        return breached

    async def overview(self, action: Any, actor: str | None = None, request_id: Any = None) -> BenchmarkOverview:
        plan = self.plans.get(action.profile_id)
        if plan is None:
            return BenchmarkOverview(profile_id=action.profile_id, available=False, presets=(), runs=())
        rows, next_cursor = self.history_page(action.profile_id, cursor=getattr(action, "cursor", None), limit=min(getattr(action, "limit", 20), 20))
        valid_runs = []
        corrupt_runs = 0
        trend_points = []
        for row in rows:
            try:
                summary = json.loads(row[8] or "{}")
                metrics = tuple(BenchmarkMetricView.model_validate(item) for item in summary.get("metrics", ()))
                baseline_diagnostics = BenchmarkDiagnosticsView.model_validate(summary["baseline_diagnostics"])
                candidate_diagnostics = BenchmarkDiagnosticsView.model_validate(summary["candidate_diagnostics"])
                valid_runs.append(BenchmarkRunSummary(
                    id=row[0], profile_id=ProfileId(row[1]), baseline_preset=row[2], candidate_preset=row[3],
                    state=row[4], created_at=row[5], finished_at=row[6], overall_verdict=row[7], metrics=metrics,
                    baseline_diagnostics=baseline_diagnostics, candidate_diagnostics=candidate_diagnostics, error_code=row[9],
                ))
                trend_points.append(BenchmarkTrendPoint(
                    id=row[0], finished_at=row[6], verdict=row[7] or "inconclusive",
                    metrics=tuple(BenchmarkTrendMetric(
                        name=item.name, baseline_median=item.baseline_median,
                        candidate_median=item.candidate_median, delta_percent=item.delta_percent,
                        verdict=item.verdict,
                    ) for item in metrics[:16]),
                ))
            except Exception:
                # A single corrupt historical JSON row must not blank the
                # entire dashboard; retain the rest and let the audit surface
                # identify the skipped run separately.
                corrupt_runs += 1
                continue
        return BenchmarkOverview(
            profile_id=action.profile_id,
            available=True,
            presets=tuple(BenchmarkPresetView(id=preset.id, label=preset.label) for preset in plan.presets),
            runs=tuple(valid_runs),
            next_cursor=next_cursor,
            corrupt_runs=corrupt_runs,
            trends=tuple(trend_points),
        )


__all__ = ["BenchmarkPlan", "BenchmarkPreset", "BenchmarkService", "parse_benchmark_plans"]
