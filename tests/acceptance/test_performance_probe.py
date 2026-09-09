import json
import subprocess
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from tools.acceptance.performance_probe import CollectorConfig, collect


class Handler(BaseHTTPRequestHandler):
    calls = []
    def do_GET(self):
        self.calls.append(self.path)
        if self.path == "/api/v1/status":
            body = b'{"profiles":[]}'
            content_type = "application/json"
        elif self.path == "/api/v1/perf":
            body = b'{"slotd":{"cycle":{"p95_ms":2}}}'
            content_type = "application/json"
        elif self.path == "/api/v1/stream":
            body = b'data: {"generation":1}\\n\\n'
            content_type = "text/event-stream"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        self.send_response(405); self.end_headers()
    def log_message(self, *_args): pass


def test_collector_uses_only_authenticated_get_surfaces_and_bounds_output():
    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = collect(CollectorConfig(f"http://127.0.0.1:{server.server_port}", "secret",
                                         duration_seconds=.2, interval_seconds=.01))
    finally:
        server.shutdown(); thread.join()
    assert result["event_loop_stall_ms"] == []
    assert result["collector"]["readOnly"] is True
    assert result["subscriber_bytes"] > 0
    assert result["connection_attempts"] is None
    assert all(path in {"/api/v1/status", "/api/v1/perf", "/api/v1/stream"} for path in Handler.calls)
    assert not any(path.startswith("POST") for path in Handler.calls)


def test_collector_optionally_records_bounded_sample_offsets_and_maintenance_sequence():
    class TimedHandler(Handler):
        counter = 0
        def do_GET(self):
            if self.path == "/api/v1/perf":
                type(self).counter += 1
                body = json.dumps({"slotd": {
                        "maintenance_ms": [float(type(self).counter)],
                        "maintenance_sequence": {"start": type(self).counter - 1, "end": type(self).counter},
                }}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), TimedHandler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = collect(CollectorConfig(
            f"http://127.0.0.1:{server.server_port}", "secret",
            duration_seconds=.08, interval_seconds=.01, include_sample_timing=True))
    finally:
        server.shutdown(); thread.join()
    assert result["collector"]["sampleTiming"] is True
    assert len(result["sample_monotonic_offsets_ms"]) == result["active_intervals"]
    assert result["sample_monotonic_offsets_ms"] == sorted(result["sample_monotonic_offsets_ms"])
    assert result["maintenance_tick_ms"] == [float(i) for i in range(2, result["active_intervals"] + 1)]
    assert [item["sequence_end"] for item in result["maintenance_observations"]] == list(range(2, result["active_intervals"] + 1))
    assert all(item["sample_offset_ms"] in result["sample_monotonic_offsets_ms"] for item in result["maintenance_observations"])


def test_collector_remains_backward_compatible_without_maintenance_fields():
    result = collect(CollectorConfig("http://localhost", "secret", duration_seconds=.001), opener=lambda *_a, **_k: None)
    assert "maintenance_tick_ms" not in result
    assert "sample_monotonic_offsets_ms" not in result
    assert "maintenanceSequence" not in result["collector"]


def test_labeled_sequence_rejects_maintenance_gap_without_clearing_event_loop():
    import pytest
    from tools.acceptance.performance_probe import _collect_sequence_values
    with pytest.raises(ValueError, match="maintenance sequence"):
        _collect_sequence_values([1.0], {"start": 4, "end": 5}, 3, label="maintenance")
    values, cursor = _collect_sequence_values([2.0], {"start": 0, "end": 1}, None, label="event-loop")
    assert values == [] and cursor == 1


def test_collector_does_not_commit_offset_or_cursor_for_rejected_iteration():
    class AdversarialHandler(Handler):
        counter = 0
        def do_GET(self):
            if self.path == "/api/v1/perf":
                type(self).counter += 1
                n = type(self).counter
                sequence = ({"start": 0, "end": 1} if n == 1 else
                            {"start": 5, "end": 6} if n == 2 else
                            {"start": n - 2, "end": n - 1})
                body = json.dumps({"slotd": {
                    "event_loop_lag_ms": [float(n)],
                    "event_loop_lag_sequence": sequence,
                }}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), AdversarialHandler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = collect(CollectorConfig(
            f"http://127.0.0.1:{server.server_port}", "secret",
            duration_seconds=.08, interval_seconds=.01, include_sample_timing=True))
    finally:
        server.shutdown(); thread.join()
    assert AdversarialHandler.counter == result["active_intervals"] + 1
    assert len(result["sample_monotonic_offsets_ms"]) == result["active_intervals"]
    assert result["event_loop_stall_ms"] == [float(i) for i in range(3, AdversarialHandler.counter + 1)]
    assert result["collector"]["eventLoopSequenceValid"] is False


def test_collector_rejects_unbounded_duration():
    import pytest
    with pytest.raises(ValueError):
        collect(CollectorConfig("http://localhost", "secret", duration_seconds=3601))


def test_collector_rejects_sequence_gaps_instead_of_truncating():
    import pytest
    from tools.acceptance.performance_probe import _collect_event_loop_values
    with pytest.raises(ValueError):
        _collect_event_loop_values([1, 2], {"start": 4, "end": 6}, 3)


def test_collector_revalidates_process_identity_and_clears_cpu_on_drift(monkeypatch):
    import psutil
    import tools.acceptance.performance_probe as module
    calls = iter([(10, "horizon.service"), (11, "horizon.service")])
    monkeypatch.setattr(module, "validate_process_identity", lambda *_args, **_kwargs: next(calls))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)
    class Process:
        def cpu_percent(self, _interval):
            return 40.0
    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = module.collect(CollectorConfig(
            f"http://127.0.0.1:{server.server_port}", "secret", duration_seconds=.05,
            interval_seconds=.01, slotd_pid=123, slotd_cgroup="horizon.service"))
    finally:
        server.shutdown(); thread.join()
    assert result["slotd_cpu_percent"] == []
    assert result["collector"]["processIdentity"] == {
        "requested": True, "valid": False, "error": True,
    }


def test_sequence_contract_preserves_300s_and_max_duration_values_exactly():
    from tools.acceptance.performance_probe import _collect_event_loop_values
    cursor = None
    collected = []
    for start in range(0, 1_200, 100):
        values, cursor = _collect_event_loop_values(
            list(range(start, start + 100)), {"start": start, "end": start + 100}, cursor)
        collected.extend(values)
    assert collected == list(range(100, 1_200))
    cursor = None
    collected = []
    for start in range(0, 14_400, 240):
        values, cursor = _collect_event_loop_values(
            list(range(start, start + 240)), {"start": start, "end": start + 240}, cursor)
        collected.extend(values)
    assert collected == list(range(240, 14_400))


def test_sequence_contract_refuses_oversize_window():
    import pytest
    from tools.acceptance.performance_probe import _collect_event_loop_values
    with pytest.raises(ValueError):
        _collect_event_loop_values([0] * 1_025, {"start": 0, "end": 1_025}, None)


def test_collector_rejects_invalid_origin_and_only_allowlisted_paths():
    import pytest
    with pytest.raises(ValueError):
        collect(CollectorConfig("file:///tmp", "secret", duration_seconds=1))
    from tools.acceptance.performance_probe import _get_json
    with pytest.raises(ValueError):
        _get_json(CollectorConfig("http://localhost", "secret"), "/api/v1/start", lambda *_a, **_k: None)


class FakeResponse:
    def __init__(self, body, content_type="application/json", url="http://localhost"):
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.url = url
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def geturl(self): return self.url
    def read(self, _limit=-1): return self.body


def test_adversarial_redirect_malformed_and_oversized_payloads_fail_closed():
    import pytest
    from tools.acceptance.performance_probe import _get_json, _sse_bytes
    config = CollectorConfig("http://localhost", "secret")
    with pytest.raises(ValueError):
        _get_json(config, "/api/v1/status",
                  lambda *_a, **_k: FakeResponse(b"{}", url="http://evil"))
    with pytest.raises(ValueError):
        _get_json(config, "/api/v1/status",
                  lambda *_a, **_k: FakeResponse(b"not-json"))
    with pytest.raises(ValueError):
        _get_json(config, "/api/v1/status",
                  lambda *_a, **_k: FakeResponse(b"{}", "text/plain"))
    with pytest.raises(ValueError):
        _sse_bytes(config, lambda *_a, **_k: FakeResponse(b"x" * 65537, "text/event-stream"))


def test_process_identity_requires_expected_cgroup_and_pins_start_time(tmp_path):
    import pytest
    from tools.acceptance.performance_probe import validate_process_identity
    proc = tmp_path / "123"
    proc.mkdir()
    (proc / "stat").write_text("123 (java) S " + " ".join(["0"] * 19))
    (proc / "cgroup").write_text("0::/system.slice/horizon.service\n")
    assert validate_process_identity(123, "horizon.service", proc_root=tmp_path)[0] == 0
    with pytest.raises(ValueError):
        validate_process_identity(123, "other.service", proc_root=tmp_path)


def test_process_cpu_is_host_capacity_normalized_once_and_rejects_invalid_count():
    import pytest
    from tools.acceptance.performance_probe import normalize_process_cpu_percent, _validated_logical_cpu_count
    assert normalize_process_cpu_percent(40.0, 4) == 10.0
    assert normalize_process_cpu_percent(10.0, 4) == 2.5
    for count in (0, -1, None, True, 4.0):
        with pytest.raises(ValueError):
            normalize_process_cpu_percent(10.0, count)
    class Psutil:
        @staticmethod
        def cpu_count(logical=True):
            return 4
    assert _validated_logical_cpu_count(Psutil) == 4


def test_cpu_api_arrays_are_ignored_and_psutil_warmup_is_separate(monkeypatch):
    import tools.acceptance.performance_probe as module
    import psutil

    class Clock:
        value = 0.0
        def monotonic(self):
            self.value += .01
            return self.value
    clock = Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "validate_process_identity", lambda *_a, **_k: (10, "horizon.service"))
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: 4)

    class Process:
        def __init__(self): self.readings = iter([40.0] + [80.0] * 100)
        def cpu_percent(self, _interval): return next(self.readings)
    monkeypatch.setattr(psutil, "Process", lambda _pid: Process())

    class Response(FakeResponse):
        def __init__(self, path):
            body = (b'{"profiles":[]}' if path.endswith("status") else
                    b'{"slotd":{"cpu_percent":[999],"event_loop_lag_ms":[],"event_loop_lag_sequence":{"start":0,"end":0}}}')
            super().__init__(body)
    result = module.collect(
        CollectorConfig("http://localhost", "secret", duration_seconds=1.0,
                        interval_seconds=.01, slotd_pid=123, slotd_cgroup="horizon.service"),
        opener=lambda request, **_kwargs: Response(request.full_url))
    assert result["cpu_warmup_observations"]
    assert result["cpu_observations"]
    assert result["slotd_cpu_percent"] == [20.0] * len(result["cpu_observations"])
    assert all(item["duration_seconds"] > 0 for item in result["cpu_warmup_observations"] + result["cpu_observations"])
    assert all(item["end_monotonic"] >= item["start_monotonic"] for item in result["cpu_warmup_observations"] + result["cpu_observations"])


def test_phase_snapshots_are_sampled_aggregates_without_profile_identity():
    from tools.acceptance.performance_probe import _sample_phase
    assert _sample_phase({"profiles": [{"state": "starting"}]}) == "startup"
    assert _sample_phase({"initializing": True, "profiles": []}) == "transition"
    assert _sample_phase({"profiles": [{"state": "running", "health": "healthy", "required_ports_ready": True, "players_online": 0}]}) == "ready-empty"
    assert _sample_phase({"profiles": [{"state": "running", "health": "healthy", "required_ports_ready": True, "players_online": 2}]}) == "gameplay"
    assert _sample_phase({"profiles": [{"state": "stopped"}]}) == "stopped"
    assert _sample_phase({"profiles": [{"state": "blocked"}]}) == "unknown"
    # A blocked profile cannot hide the active owner's sampled phase.
    assert _sample_phase({"profiles": [
        {"state": "blocked", "slot_owner": "private"},
        {"state": "running", "health": "healthy", "required_ports_ready": True, "players_online": 0},
    ]}) == "ready-empty"


def test_collector_rejects_nonfinite_probe_windows():
    import math
    import pytest
    for duration, interval in ((math.nan, 1), (1, math.inf), (True, 1)):
        with pytest.raises(ValueError):
            collect(CollectorConfig("http://localhost", "secret", duration_seconds=duration,
                                    interval_seconds=interval), opener=lambda *_a, **_k: None)


def _browser_v2_fixture():
    return {
        "schemaVersion": "phase2.1.browser.v2", "status_ui_latency_ms": [10, 20, 30],
        "hidden_tab_requests": 0, "hidden_request_grace_s": 1.0,
        "status_calls_total": 4, "status_calls_while_stream_healthy": 0,
        "status_calls_while_stream_degraded": 1, "resume_authoritative_status_requests": 1,
        "resume_stream_opens": 1, "subscriber_bytes": 100,
        "reconnects": 2, "forced_reconnect": True, "forced_reconnect_delta": 1,
        "forced_reconnect_observed": True, "unique_stream_ids": 3,
        "unique_stream_sequences": 3, "unique_stream_generations": 3,
        "stream_sequences_ordered": True, "stream_generations_non_decreasing": True,
        "duplicate_stream_ids": 0,
        "browser": {"streamOpens": 3, "streamCloses": 2, "streamErrors": 1, "readOnly": True},
        "thresholds": {"hidden_requests_after_grace": 0, "duplicate_stream_ids": 0,
                        "status_calls_while_stream_healthy": 0,
                        "resume_authoritative_status_requests": 1, "resume_stream_opens": 1},
    }


def test_browser_v2_parser_derives_natural_reconnects_only_and_rejects_inconsistency():
    import runpy
    import pytest
    parser = runpy.run_path(str(Path(__file__).parents[2] / "tools/acceptance/performance_collect.py"))["_browser_v2"]
    mapped, checks = parser(_browser_v2_fixture())
    assert mapped["reconnects"] == 1
    assert mapped["connection_attempts"] == 2
    assert checks["forced_reconnect"]["pass"]
    bad = _browser_v2_fixture()
    bad["forced_reconnect_observed"] = False
    with pytest.raises(ValueError):
        parser(bad)
    bad = _browser_v2_fixture()
    bad["forced_reconnect_delta"] = 3
    bad["reconnects"] = 3
    with pytest.raises(ValueError):
        parser(bad)
    for key, value in (("hidden_request_grace_s", float("nan")),
                       ("hidden_request_grace_s", float("inf")),
                       ("hidden_request_grace_s", -1.0),
                       ("status_ui_latency_ms", [float("nan")]),
                       ("status_ui_latency_ms", [float("inf")]),
                       ("status_ui_latency_ms", [-1.0])):
        bad = _browser_v2_fixture(); bad[key] = value
        with pytest.raises(ValueError):
            parser(bad)
    bad = _browser_v2_fixture()
    bad["browser"] = {**bad["browser"], "streamOpens": 1}
    with pytest.raises(ValueError):
        parser(bad)


def test_phase2_collect_cli_merges_v2_and_fails_closed_on_adversarial_v2(tmp_path):
    import pytest
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    token = tmp_path / "token"; token.write_text("secret\n")
    evidence = tmp_path / "browser.json"; evidence.write_text(json.dumps(_browser_v2_fixture()))
    output = tmp_path / "report.json"
    script = Path(__file__).parents[2] / "tools/acceptance/performance_collect.py"
    try:
        result = subprocess.run([
            sys.executable, str(script), "--base-url", f"http://127.0.0.1:{server.server_port}",
            "--token-file", str(token), "--duration", ".2", "--interval", ".01",
            "--output", str(output), "--browser-evidence", str(evidence),
        ], capture_output=True, text=True)
        assert result.returncode == 2  # CPU and event-loop coverage are intentionally absent here.
        report = json.loads(output.read_text())
        assert report["inputs"]["reconnects"] == 1
        assert report["inputs"]["connection_attempts"] == 2
        assert report["browserChecks"]["resume_stream_opens"]["pass"]
        collector = report["inputs"]["collector"]
        assert collector["cpuNormalization"] == {
            "applied": False, "logicalCpuCount": None, "source": None, "formula": None,
        }
        assert collector["processIdentity"] == {"requested": False, "valid": False, "error": False}
        assert not {"token", "tokenUsed", "pid", "cgroup", "startTime"} & set(collector)
        bad = _browser_v2_fixture(); bad["forced_reconnect_delta"] = 3
        evidence.write_text(json.dumps(bad))
        rejected = subprocess.run([
            sys.executable, str(script), "--base-url", f"http://127.0.0.1:{server.server_port}",
            "--token-file", str(token), "--duration", ".1", "--interval", ".01",
            "--output", str(tmp_path / "bad-report.json"), "--browser-evidence", str(evidence),
        ], capture_output=True, text=True)
        assert rejected.returncode != 0
        assert "forced reconnect delta must equal one" in rejected.stderr
    finally:
        server.shutdown(); thread.join()


def test_performance_collect_is_source_only_and_executable():
    script = Path(__file__).parents[2] / "tools/acceptance/performance_collect.py"
    assert script.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/env python3"
    assert script.stat().st_mode & 0o111
