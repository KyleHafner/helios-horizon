import json
import subprocess
import sys
from pathlib import Path

import pytest

from game_control.phase2_threshold import evaluate_thresholds, samples_from_mapping


def complete(**overrides):
    data = dict(
        status_ui_latency_ms=[100, 200, 300], slotd_cpu_percent=[.2, .4, .6],
        web_cpu_percent=[.1, .2, .3], event_loop_stall_ms=[1, 2, 3],
        reconnects=1, connection_attempts=100, subscriber_bytes=1000, subscribers=1,
        active_intervals=3, inactive_intervals=2, hidden_tab_requests=0,
    )
    data.update(overrides)
    return samples_from_mapping(data)


def test_complete_coverage_skips_push_and_preserves_inactive_state():
    report = evaluate_thresholds(complete())
    assert report["decision"] == "SKIP_PUSH"
    assert report["readOnly"] is True
    assert report["coverage"] == {"activeIntervals": 3, "inactiveIntervals": 2,
                                  "hiddenTabRequests": 0}


def test_missing_input_is_inconclusive_not_a_pass():
    report = evaluate_thresholds(complete(reconnects=None, connection_attempts=None))
    assert report["decision"] == "INCONCLUSIVE"
    assert "reconnect_rate" in report["missing"]


def test_failed_input_justifies_push():
    report = evaluate_thresholds(complete(event_loop_stall_ms=[20, 30, 40]))
    assert report["decision"] == "JUSTIFY_PUSH"
    assert "event_loop_stall_p99_ms" in report["failed"]


def test_hidden_tab_requests_are_explicitly_observable():
    report = evaluate_thresholds(complete(hidden_tab_requests=4))
    assert report["coverage"]["hiddenTabRequests"] == 4
    assert report["decision"] == "JUSTIFY_PUSH"


def test_hidden_tab_measurement_is_required_and_minimum_coverage_is_enforced():
    assert evaluate_thresholds(complete(hidden_tab_requests=None))["decision"] == "INCONCLUSIVE"
    assert evaluate_thresholds(complete(active_intervals=0))["decision"] == "INCONCLUSIVE"


def test_percentiles_need_three_samples():
    report = evaluate_thresholds(complete(status_ui_latency_ms=[1]))
    assert report["decision"] == "INCONCLUSIVE"
    assert "status_ui_p95_ms" in report["missing"]


def test_input_limits_and_invalid_counts_fail_closed():
    with pytest.raises(ValueError):
        samples_from_mapping({"status_ui_latency_ms": [1] * 10001})
    with pytest.raises(ValueError):
        evaluate_thresholds(complete(subscribers=0))
    with pytest.raises(ValueError):
        evaluate_thresholds(complete(active_intervals=-1))


def test_event_loop_max_duration_contract_is_bounded_and_exact():
    values = list(range(16_384))
    samples = complete(event_loop_stall_ms=values)
    report = evaluate_thresholds(samples)
    assert report["checks"]["event_loop_stall_p99_ms"]["value"] == pytest.approx(16_219.17)
    with pytest.raises(ValueError):
        samples_from_mapping({"event_loop_stall_ms": values + [16_384]})


def test_event_loop_percentile_is_deterministic_for_300s_shape():
    values = [float(i % 17) for i in range(1_200)]
    first = evaluate_thresholds(complete(event_loop_stall_ms=values))
    second = evaluate_thresholds(complete(event_loop_stall_ms=list(values)))
    assert first == second


def test_invalid_collector_sequence_is_refused_not_evaluated():
    with pytest.raises(ValueError):
        samples_from_mapping({"event_loop_stall_ms": [1, 2, 3],
                              "collector": {"eventLoopSequenceValid": False}})


def test_cli_is_read_only_and_writes_report(tmp_path):
    fixture = tmp_path / "samples.json"
    output = tmp_path / "report.json"
    fixture.write_text(json.dumps({
        "status_ui_latency_ms": [1, 1, 1], "slotd_cpu_percent": [1, 1, 1],
        "web_cpu_percent": [1, 1, 1], "event_loop_stall_ms": [1, 1, 1],
        "reconnects": 0, "connection_attempts": 1, "subscriber_bytes": 1,
        "subscribers": 1, "active_intervals": 3, "hidden_tab_requests": 0,
    }))
    script = Path(__file__).parents[1] / "ops/bin/horizon-phase2-threshold"
    result = subprocess.run([sys.executable, str(script), str(fixture), "--output", str(output)],
                            capture_output=True, text=True, check=True)
    assert not result.stdout
    assert json.loads(output.read_text())["decision"] == "SKIP_PUSH"
    assert not list(tmp_path.glob("*.tmp"))
