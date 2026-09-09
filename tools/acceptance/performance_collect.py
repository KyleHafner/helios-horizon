#!/usr/bin/env python3
"""Collect bounded Phase 2.1 read-only HTTP/SSE inputs (api/v1/status; readOnly) and evaluate them."""
from __future__ import annotations
import argparse, hashlib, json
import math
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from tools.acceptance.performance_probe import MAX_BODY, CollectorConfig, collect  # noqa: E402
from tools.acceptance.cpu_phase_report import summarize_cpu_phases  # noqa: E402
from tools.acceptance.performance_thresholds import evaluate_thresholds, samples_from_mapping  # noqa: E402


BROWSER_V2_ALLOWED = {
    "schemaVersion", "status_ui_latency_ms", "hidden_tab_requests", "hidden_request_grace_s",
    "status_calls_total", "status_calls_while_stream_healthy", "status_calls_while_stream_degraded",
    "resume_authoritative_status_requests", "resume_stream_opens", "subscriber_bytes", "reconnects",
    "forced_reconnect", "forced_reconnect_delta", "forced_reconnect_observed", "unique_stream_ids",
    "unique_stream_sequences", "unique_stream_generations", "stream_sequences_ordered",
    "stream_generations_non_decreasing", "duplicate_stream_ids", "browser", "thresholds",
}


def _sanitized_collector_metadata(collector: object) -> dict:
    """Persist only bounded, non-identifying collector contract metadata.

    ``collect`` owns the raw observation object.  The report is an external
    evidence artifact, so keep the explicit contract fields while excluding
    credentials and process identity material (PIDs/cgroups/start times).
    """
    if not isinstance(collector, dict):
        raise ValueError("collector metadata must be an object")
    cpu = collector.get("cpuNormalization")
    identity = collector.get("processIdentity")
    if not isinstance(cpu, dict) or not isinstance(identity, dict):
        raise ValueError("collector metadata is missing required coverage")
    cpu_keys = {"applied", "logicalCpuCount", "source", "formula"}
    identity_keys = {"requested", "valid", "error"}
    if (not {"applied", "logicalCpuCount", "source"} <= set(cpu)
            or not set(cpu) <= cpu_keys or set(identity) != identity_keys):
        raise ValueError("collector metadata contains unapproved fields")
    if (not isinstance(cpu["applied"], bool)
            or (cpu["logicalCpuCount"] is not None
                and (not isinstance(cpu["logicalCpuCount"], int)
                     or isinstance(cpu["logicalCpuCount"], bool)
                     or not 0 < cpu["logicalCpuCount"] <= 4096))
            or not isinstance(cpu["source"], (str, type(None)))
            or not isinstance(cpu.get("formula"), (str, type(None)))):
        raise ValueError("collector CPU normalization metadata is invalid")
    if not all(isinstance(identity[key], bool) for key in identity_keys):
        raise ValueError("collector process identity metadata is invalid")
    if (collector.get("schemaVersion") != "phase2.1.inputs.v1"
            or not isinstance(collector.get("samples"), int)
            or isinstance(collector["samples"], bool) or not 0 <= collector["samples"] <= 1200
            or not isinstance(collector.get("errors"), int)
            or isinstance(collector["errors"], bool) or not 0 <= collector["errors"] <= 1200
            or collector.get("readOnly") is not True
            or collector.get("maxBodyBytes") != MAX_BODY
            or collector.get("eventLoopSequence") is not True
            or not isinstance(collector.get("eventLoopSequenceValid"), bool)
            or collector.get("maxEventLoopWindow") != 1024
            or collector.get("maxEventLoopValues") != 16384):
        raise ValueError("collector metadata is invalid")
    return {"schemaVersion": collector["schemaVersion"], "samples": collector["samples"],
            "errors": collector["errors"], "readOnly": True, "maxBodyBytes": MAX_BODY,
            "eventLoopSequence": True, "eventLoopSequenceValid": collector["eventLoopSequenceValid"],
            "maxEventLoopWindow": 1024, "maxEventLoopValues": 16384,
            "cpuNormalization": {"applied": cpu["applied"],
                                  "logicalCpuCount": cpu["logicalCpuCount"],
                                  "source": cpu["source"], "formula": cpu.get("formula")},
            "processIdentity": {key: identity[key] for key in ("requested", "valid", "error")}}


def _browser_v2(browser: dict) -> tuple[dict, dict]:
    """Validate the browser contract and return threshold inputs plus coverage checks."""
    required = BROWSER_V2_ALLOWED - {"status_calls_total", "status_calls_while_stream_degraded",
                                      "unique_stream_ids", "unique_stream_generations"}
    if browser.get("schemaVersion") != "phase2.1.browser.v2" or set(browser) != BROWSER_V2_ALLOWED:
        raise ValueError("browser evidence must exactly match the phase2.1.browser.v2 allowlist")
    if not isinstance(browser["status_ui_latency_ms"], list) or len(browser["status_ui_latency_ms"]) > 1000:
        raise ValueError("browser latency evidence is invalid")
    for value in browser["status_ui_latency_ms"]:
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value)) or value < 0):
            raise ValueError("browser latency evidence is invalid")
    int_fields = required - {"schemaVersion", "status_ui_latency_ms", "forced_reconnect",
                             "forced_reconnect_observed", "stream_sequences_ordered",
                             "stream_generations_non_decreasing", "browser", "thresholds",
                             "hidden_request_grace_s"}
    for key in int_fields | {"unique_stream_ids", "unique_stream_generations"}:
        if not isinstance(browser[key], int) or isinstance(browser[key], bool) or browser[key] < 0:
            raise ValueError(f"browser {key} evidence is invalid")
    for key in ("forced_reconnect", "forced_reconnect_observed", "stream_sequences_ordered",
                "stream_generations_non_decreasing"):
        if not isinstance(browser[key], bool):
            raise ValueError(f"browser {key} evidence is invalid")
    if (not isinstance(browser["hidden_request_grace_s"], (int, float))
            or isinstance(browser["hidden_request_grace_s"], bool)
            or not math.isfinite(float(browser["hidden_request_grace_s"]))
            or browser["hidden_request_grace_s"] <= 0):
        raise ValueError("browser hidden grace is invalid")
    nested = browser["browser"]
    if not isinstance(nested, dict) or set(nested) != {"streamOpens", "streamCloses", "streamErrors", "readOnly"}:
        raise ValueError("browser stream accounting is invalid")
    if (not all(isinstance(nested[k], int) and not isinstance(nested[k], bool) and nested[k] >= 0
                for k in ("streamOpens", "streamCloses", "streamErrors"))
            or nested["streamOpens"] <= 0 or not isinstance(nested["readOnly"], bool)
            or not nested["readOnly"]):
        raise ValueError("browser stream accounting is invalid")
    thresholds = browser["thresholds"]
    expected_thresholds = {"hidden_requests_after_grace": 0, "duplicate_stream_ids": 0,
                           "status_calls_while_stream_healthy": 0,
                           "resume_authoritative_status_requests": 1, "resume_stream_opens": 1}
    if thresholds != expected_thresholds:
        raise ValueError("browser thresholds are not the official v2 contract")
    # The browser helper performs exactly one causal forced failure.  Only that
    # one event may be excluded from the unexpected reconnect numerator.
    if not browser["forced_reconnect"] or not browser["forced_reconnect_observed"]:
        raise ValueError("forced reconnect evidence is missing")
    if browser["forced_reconnect_delta"] != 1:
        raise ValueError("forced reconnect delta must equal one")
    if browser["reconnects"] < browser["forced_reconnect_delta"]:
        raise ValueError("forced reconnect delta exceeds reconnect total")
    if browser["stream_sequences_ordered"] is not True or browser["stream_generations_non_decreasing"] is not True:
        raise ValueError("stream ordering evidence is invalid")
    if browser["duplicate_stream_ids"] != 0:
        raise ValueError("duplicate stream evidence is invalid")
    opens = nested["streamOpens"]
    if (browser["unique_stream_sequences"] <= 0 or browser["unique_stream_generations"] <= 0
            or nested["streamCloses"] > opens
            or browser["status_calls_total"] < browser["status_calls_while_stream_healthy"] + browser["status_calls_while_stream_degraded"]
            or browser["resume_authoritative_status_requests"] > browser["status_calls_total"]
            or opens < 2 or browser["reconnects"] > opens - 1
            or nested["streamErrors"] < browser["forced_reconnect_delta"]):
        raise ValueError("stream-open accounting is inconsistent")
    if browser["hidden_tab_requests"] != 0 or browser["status_calls_while_stream_healthy"] != 0:
        raise ValueError("browser hidden/healthy-stream checks failed")
    if browser["resume_authoritative_status_requests"] != 1 or browser["resume_stream_opens"] != 1:
        raise ValueError("browser visibility resume checks failed")
    forced_reconnects = 1
    mapped = {
        "status_ui_latency_ms": browser["status_ui_latency_ms"],
        "hidden_tab_requests": browser["hidden_tab_requests"],
        "subscriber_bytes": browser["subscriber_bytes"],
        "reconnects": browser["reconnects"] - forced_reconnects,
        "connection_attempts": opens - 1,
        "subscribers": 1,
    }
    checks = {
        "forced_reconnect": {"covered": True, "pass": True, "value": True},
        "stream_sequences": {"covered": True, "pass": True, "value": True},
        "stream_generations": {"covered": True, "pass": True, "value": True},
        "duplicate_stream_ids": {"covered": True, "pass": True, "value": 0},
        "resume_authoritative_status_requests": {"covered": True, "pass": True, "value": 1},
        "resume_stream_opens": {"covered": True, "pass": True, "value": 1},
    }
    return mapped, checks

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--slotd-pid", type=int)
    parser.add_argument("--web-pid", type=int)
    parser.add_argument("--slotd-cgroup")
    parser.add_argument("--web-cgroup")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--browser-evidence", type=Path)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("token file is empty")
    inputs = collect(CollectorConfig(args.base_url, token, args.duration, args.interval,
                                     slotd_pid=args.slotd_pid, web_pid=args.web_pid,
                                     slotd_cgroup=args.slotd_cgroup, web_cgroup=args.web_cgroup))
    browser = {}
    if args.browser_evidence:
        if args.browser_evidence.stat().st_size > 2 * 1024 * 1024:
            raise SystemExit("browser evidence is oversized")
        browser = json.loads(args.browser_evidence.read_text(encoding="utf-8"))
        if not isinstance(browser, dict):
            raise SystemExit("browser evidence must be an object")
        mapped, browser_checks = _browser_v2(browser)
        inputs.update(mapped)
    else:
        browser_checks = {}
    collector_metadata = inputs.get("collector", {})
    cpu_normalization = collector_metadata.get("cpuNormalization") if isinstance(collector_metadata, dict) else None
    cpu_phase_input = {
        "cpu_observations": inputs.get("cpu_observations", []),
        "cpu_warmup_observations": inputs.get("cpu_warmup_observations", []),
        "process_identity": collector_metadata.get("processIdentity") if isinstance(collector_metadata, dict) else None,
    }
    if cpu_normalization is not None:
        # The reporter validates this allowlisted metadata independently; no
        # process IDs, cgroups, or raw telemetry enter the additive summary.
        cpu_phase_input["cpu_normalization"] = cpu_normalization
    cpu_phase_summary = summarize_cpu_phases(cpu_phase_input)
    report = evaluate_thresholds(samples_from_mapping(inputs))
    report["browserChecks"] = browser_checks
    report["cpuPhaseSummary"] = cpu_phase_summary
    report["inputs"] = inputs
    report["evidenceHashes"] = {
        "collectorInputs": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
        "browserEvidence": hashlib.sha256(args.browser_evidence.read_bytes()).hexdigest()
        if args.browser_evidence else None,
    }
    report["inputs"]["tokenUsed"] = True
    report["inputs"]["collector"] = _sanitized_collector_metadata(inputs.get("collector"))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(args.output)
    return 0 if report["decision"] != "INCONCLUSIVE" else 2

if __name__ == "__main__":
    raise SystemExit(main())
