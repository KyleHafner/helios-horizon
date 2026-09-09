import pytest
import json
import subprocess
import sys
from pathlib import Path

from tools.acceptance.cpu_phase_report import summarize_cpu_phases


NORM = {"applied": True, "logicalCpuCount": 4, "source": "psutil_process_cpu_percent", "formula": "raw_percent / logical_cpu_count"}
IDENTITY = {"requested": True, "valid": True, "error": False}


def row(value, start, phase="gameplay", process="slotd"):
    return {"process": process, "cpu_percent": value, "start_monotonic": start,
            "end_monotonic": start + 1, "duration_seconds": 1, "phase": phase}


def test_groups_phases_and_preserves_peaks_with_percentiles():
    result = summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [
        row(10, 0, "startup"), row(20, 1), row(30, 2), row(40, 3), row(99, 4, "ready-empty"),
    ], "cpu_warmup_observations": [row(100, 5)]})
    assert result["status"] == "available"
    assert result["processes"]["slotd"]["gameplay"] == {"count": 3, "duration_seconds": 3.0, "median": 30.0, "p95": 30.0, "max": 40.0}
    assert result["processes"]["slotd"]["ready-empty"]["max"] == 99.0
    assert result["processes"]["slotd"]["startup"]["count"] == 1
    assert result["processes"]["slotd"]["startup"]["median"] is None


def test_unknown_phase_is_retained_and_legacy_timing_is_unavailable():
    result = summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [row(4, 0, "unknown"), row(5, 1, "transition"), row(6, 2, "stopped")]})
    assert result["processes"]["slotd"]["unknown"]["count"] == 1
    assert result["processes"]["slotd"]["transition"]["count"] == 1
    assert summarize_cpu_phases({"cpu_observations": [{"process": "slotd", "cpu_percent": 4}]})["reason"] == "legacy missing timing"
    assert summarize_cpu_phases({"slotd_cpu_percent": [4], "web_cpu_percent": []})["reason"] == "legacy missing timing"


def test_no_samples_and_malformed_inputs_fail_closed():
    assert summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [], "cpu_warmup_observations": []})["status"] == "unavailable"
    with pytest.raises(ValueError):
        summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [dict(row(1, 0), secret="x")]})
    with pytest.raises(ValueError):
        summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [row(float("nan"), 0)]})
    with pytest.raises(ValueError):
        summarize_cpu_phases({"cpu_normalization": NORM, "process_identity": IDENTITY, "cpu_observations": [row(1, 0, "bogus")]})


def test_cli_emits_additive_summary_and_refuses_input_overwrite(tmp_path):
    payload = {"cpu_normalization": NORM, "process_identity": IDENTITY,
               "cpu_observations": [row(1, 0), row(2, 1), row(3, 2)]}
    source = tmp_path / "collector.json"
    output = tmp_path / "summary.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    script = Path(__file__).parents[2] / "tools/acceptance/cpu_phase_report.py"
    completed = subprocess.run([sys.executable, str(script), str(source), "--output", str(output)], capture_output=True, text=True)
    assert completed.returncode == 0
    assert json.loads(output.read_text())["processes"]["slotd"]["gameplay"]["count"] == 3
    rejected = subprocess.run([sys.executable, str(script), str(source), "--output", str(source)], capture_output=True, text=True)
    assert rejected.returncode != 0


def test_real_collector_to_performance_collect_report(tmp_path, monkeypatch):
    """Exercise the real collector and CLI wiring with bounded synthetic sources."""
    import tools.acceptance.performance_probe as probe
    import tools.acceptance.performance_collect as collect_cli
    import psutil

    clock = iter([float(i) * .25 for i in range(200)])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)
    calls = {"status": 0}

    def fake_json(_config, path, _opener):
        if path.endswith("status"):
            calls["status"] += 1
            phase = ("starting" if calls["status"] == 1 else
                     "running" if calls["status"] < 4 else "stopped")
            profile = {"state": phase}
            if phase == "running":
                profile.update({"required_ports_ready": True, "health": "healthy", "players_online": 1})
            return 1.0, {"profiles": [profile]}
        return 1.0, {"slotd": {}}

    monkeypatch.setattr(probe, "_get_json", fake_json)
    monkeypatch.setattr(probe, "_sse_bytes", lambda *_args: (4, True))
    monkeypatch.setattr(probe, "validate_process_identity", lambda *_args, **_kwargs: (1, "horizon.service"))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)

    class FakeProcess:
        def __init__(self, pid): self.value = 10.0 if pid == 11 else 20.0
        def cpu_percent(self, _interval): return self.value

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    source = tmp_path / "token"
    source.write_text("secret\n", encoding="utf-8")
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["performance_collect.py", "--base-url", "http://example.com",
                                       "--token-file", str(source), "--duration", "6", "--interval", "1",
                                       "--slotd-pid", "11", "--web-pid", "12", "--slotd-cgroup", "horizon.service",
                                       "--web-cgroup", "horizon.service", "--output", str(output)])
    assert collect_cli.main() == 2  # threshold decision remains inconclusive in this synthetic run
    summary = json.loads(output.read_text(encoding="utf-8"))["cpuPhaseSummary"]
    assert summary["status"] == "available"
    assert summary["processes"]["slotd"]["gameplay"]["count"] >= 1
    assert summary["processes"]["web"]["gameplay"]["count"] >= 1
    assert summary["processes"]["slotd"]["all"]["count"] == summary["processes"]["web"]["all"]["count"]
