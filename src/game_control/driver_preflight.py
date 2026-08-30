"""Closed driver ``--check`` and frozen provenance contract."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Protocol

from .errors import SafeError
from .protocol import BenchmarkDiagnosticsView, BenchmarkMetricView


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CATEGORIES = frozenset({"none", "driver", "timeout", "incomplete", "degraded", "cancelled", "cleanup"})
_PREFLIGHT_REQUIRED = frozenset({
    "schemaVersion", "driverSha256", "configSha256", "presetDigests",
    "presetArgvDigests", "pairPlan", "primaryEndpoints", "thresholds",
    "statisticsImplementation",
})
_PREFLIGHT_OPTIONAL = frozenset({
    "pairs", "baselinePreset", "candidatePreset", "overallVerdict",
    "metrics", "diagnostics", "failureCategory", "plannedPairs",
    "completedPairs",
})


class DriverPlan(Protocol):
    driver: Path
    config: Path
    presets: tuple[Any, ...]
    timeout_seconds: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _preset_digest(preset: Any) -> str:
    payload = json.dumps(
        {"id": preset.id, "label": preset.label},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SafeError("benchmark_failed", f"benchmark {label} digest is invalid")


def _require_bounded_text(value: Any, label: str, *, limit: int = 256) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or "\x00" in value or "\n" in value:
        raise SafeError("benchmark_failed", f"benchmark {label} is invalid")


def _require_finite_number(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SafeError("benchmark_failed", f"benchmark {label} is invalid")


def _validate_preflight_payload(preflight: Any, preset_ids: set[str]) -> None:
    """Validate the bounded JSON contract returned by the driver's ``--check``."""
    if not isinstance(preflight, dict) or not _PREFLIGHT_REQUIRED.issubset(preflight):
        raise SafeError("benchmark_failed", "benchmark preflight provenance is incomplete")
    if set(preflight) - (_PREFLIGHT_REQUIRED | _PREFLIGHT_OPTIONAL):
        raise SafeError("benchmark_failed", "benchmark preflight provenance has unexpected fields")
    if preflight.get("schemaVersion") != 2 or not isinstance(preflight.get("schemaVersion"), int):
        raise SafeError("benchmark_failed", "benchmark preflight schema is invalid")
    _require_sha256(preflight.get("driverSha256"), "driver")
    _require_sha256(preflight.get("configSha256"), "config")
    for field in ("presetDigests", "presetArgvDigests"):
        values = preflight.get(field)
        if not isinstance(values, dict) or set(values) != preset_ids:
            raise SafeError("benchmark_failed", f"benchmark preflight {field} is invalid")
        for preset_id, digest in values.items():
            _require_bounded_text(preset_id, "preset ID", limit=32)
            _require_sha256(digest, f"{field} for {preset_id}")
    pair_plan = preflight.get("pairPlan")
    if (
        not isinstance(pair_plan, dict)
        or set(pair_plan) != {"minimumCompletePairs", "maximumPairs"}
        or any(isinstance(pair_plan.get(key), bool) or not isinstance(pair_plan.get(key), int)
               for key in ("minimumCompletePairs", "maximumPairs"))
        or not 1 <= pair_plan["minimumCompletePairs"] <= pair_plan["maximumPairs"] <= 64
    ):
        raise SafeError("benchmark_failed", "benchmark preflight pair plan is invalid")
    endpoints = preflight.get("primaryEndpoints")
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 16:
        raise SafeError("benchmark_failed", "benchmark preflight endpoints are invalid")
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or set(endpoint) - {"name", "effectThreshold"}:
            raise SafeError("benchmark_failed", "benchmark preflight endpoint is invalid")
        _require_bounded_text(endpoint.get("name"), "endpoint name", limit=128)
        if "effectThreshold" in endpoint:
            _require_finite_number(endpoint["effectThreshold"], "endpoint threshold")
    thresholds = preflight.get("thresholds")
    if not isinstance(thresholds, dict) or not 1 <= len(thresholds) <= 16:
        raise SafeError("benchmark_failed", "benchmark preflight thresholds are invalid")
    for name, threshold in thresholds.items():
        _require_bounded_text(name, "threshold name", limit=128)
        _require_finite_number(threshold, "threshold")
    implementation = preflight.get("statisticsImplementation")
    if not isinstance(implementation, dict):
        raise SafeError("benchmark_failed", "benchmark preflight statistics implementation is invalid")
    if set(implementation) - {"name", "fixtures", "version"} or "name" not in implementation or "fixtures" not in implementation:
        raise SafeError("benchmark_failed", "benchmark preflight statistics implementation is invalid")
    for key, value in implementation.items():
        _require_bounded_text(value, f"statistics implementation {key}")
    pairs = preflight.get("pairs")
    if pairs is not None:
        if not isinstance(pairs, list) or len(pairs) > 64:
            raise SafeError("benchmark_failed", "benchmark preflight pairs are invalid")
        for pair in pairs:
            if not isinstance(pair, dict) or set(pair) - {"complete", "degraded"}:
                raise SafeError("benchmark_failed", "benchmark preflight pair is invalid")
            if not isinstance(pair.get("complete"), bool) or not isinstance(pair.get("degraded"), bool):
                raise SafeError("benchmark_failed", "benchmark preflight pair is invalid")
    if "baselinePreset" in preflight and preflight["baselinePreset"] not in preset_ids:
        raise SafeError("benchmark_failed", "benchmark preflight baseline preset is invalid")
    if "candidatePreset" in preflight and preflight["candidatePreset"] not in preset_ids:
        raise SafeError("benchmark_failed", "benchmark preflight candidate preset is invalid")
    if "overallVerdict" in preflight and preflight["overallVerdict"] not in {"better", "worse", "mixed", "inconclusive"}:
        raise SafeError("benchmark_failed", "benchmark preflight verdict is invalid")
    if "failureCategory" in preflight and preflight["failureCategory"] not in _FAILURE_CATEGORIES:
        raise SafeError("benchmark_failed", "benchmark preflight failure category is invalid")
    for field in ("plannedPairs", "completedPairs"):
        if field in preflight and (isinstance(preflight[field], bool) or not isinstance(preflight[field], int) or not 0 <= preflight[field] <= 64):
            raise SafeError("benchmark_failed", f"benchmark preflight {field} is invalid")
    if "metrics" in preflight:
        metrics = preflight["metrics"]
        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 64:
            raise SafeError("benchmark_failed", "benchmark preflight metrics are invalid")
        for metric in metrics:
            try:
                BenchmarkMetricView.model_validate(metric)
            except Exception as exc:
                raise SafeError("benchmark_failed", "benchmark preflight metric is invalid") from exc
    if "diagnostics" in preflight:
        diagnostics = preflight["diagnostics"]
        if not isinstance(diagnostics, dict) or set(diagnostics) != {"baseline", "candidate"}:
            raise SafeError("benchmark_failed", "benchmark preflight diagnostics are invalid")
        try:
            BenchmarkDiagnosticsView.model_validate(diagnostics["baseline"])
            BenchmarkDiagnosticsView.model_validate(diagnostics["candidate"])
        except Exception as exc:
            raise SafeError("benchmark_failed", "benchmark preflight diagnostics are invalid") from exc


def _validate_frozen_provenance(action: Any, frozen: Any) -> None:
    required = {"version", "driverSha256", "configSha256", "presetDigests", "presetArgvDigests", "preflight"}
    if not isinstance(frozen, dict) or set(frozen) != required or frozen.get("version") != 2:
        raise SafeError("benchmark_failed", "benchmark preflight provenance is incomplete")
    _require_sha256(frozen.get("driverSha256"), "driver")
    _require_sha256(frozen.get("configSha256"), "config")
    preset_ids = {action.baseline_preset, action.candidate_preset}
    preset_digests = frozen.get("presetDigests")
    if not isinstance(preset_digests, dict) or set(preset_digests) != preset_ids:
        raise SafeError("benchmark_failed", "benchmark preset provenance does not match the request")
    for preset_id, digest in preset_digests.items():
        _require_bounded_text(preset_id, "preset ID", limit=32)
        _require_sha256(digest, f"preset {preset_id}")
    argv_digests = frozen.get("presetArgvDigests")
    if not isinstance(argv_digests, dict) or set(argv_digests) != preset_ids:
        raise SafeError("benchmark_failed", "benchmark argv provenance does not match the request")
    for preset_id, digest in argv_digests.items():
        _require_bounded_text(preset_id, "preset ID", limit=32)
        _require_sha256(digest, f"preset argv {preset_id}")
    preflight = frozen.get("preflight")
    _validate_preflight_payload(preflight, preset_ids)
    for key in ("driverSha256", "configSha256", "presetDigests", "presetArgvDigests"):
        if preflight.get(key) != frozen[key]:
            raise SafeError("benchmark_failed", f"benchmark frozen {key} does not match preflight")


def _secure_input(path: Path, *, executable: bool = False) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SafeError("benchmark_unavailable", "benchmark executable input is unavailable")
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SafeError("benchmark_unavailable", "benchmark executable input is not root-controlled")
    if executable and not os.access(path, os.X_OK):
        raise SafeError("benchmark_unavailable", "benchmark driver is not executable")
    return path


class DriverPreflight:
    """Run/check/freeze driver provenance with no database access."""

    def run(
        self,
        plan: DriverPlan,
        job_id: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]],
        runner_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del job_id
        driver = _secure_input(plan.driver, executable=True)
        config = _secure_input(plan.config)
        result = runner(
            [str(driver), "--config", str(config), "--check"], cwd="/",
            env={"HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=min(plan.timeout_seconds, 300), check=False,
            **dict(runner_options or {}),
        )
        if result.returncode != 0:
            raise SafeError("benchmark_failed", "benchmark driver preflight failed")
        try:
            decoded = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SafeError("benchmark_failed", "benchmark preflight provenance is invalid") from exc
        if not isinstance(decoded, dict) or decoded.get("schemaVersion") != 2 or any(key not in decoded for key in _PREFLIGHT_REQUIRED):
            raise SafeError("benchmark_failed", "benchmark preflight provenance is incomplete")
        return decoded

    def freeze(self, action: Any, plan: DriverPlan, preflight: Mapping[str, Any]) -> dict[str, Any]:
        preset_ids = {action.baseline_preset, action.candidate_preset}
        provenance = {
            "version": 2,
            "driverSha256": _sha256(plan.driver),
            "configSha256": _sha256(plan.config),
            "presetDigests": {
                preset.id: _preset_digest(preset)
                for preset in plan.presets if preset.id in preset_ids
            },
            "presetArgvDigests": {
                preset_id: preflight["presetArgvDigests"][preset_id]
                for preset_id in (action.baseline_preset, action.candidate_preset)
            },
            "preflight": preflight,
        }
        frozen = json.loads(json.dumps(provenance, separators=(",", ":"), sort_keys=True, allow_nan=False))
        _validate_frozen_provenance(action, frozen)
        return frozen


__all__ = [
    "DriverPreflight", "_preset_digest", "_secure_input", "_sha256",
    "_validate_frozen_provenance", "_validate_preflight_payload",
]
