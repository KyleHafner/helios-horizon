import json
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/phase2-browser-evidence.py"


def _node_stream_probe(events: list[dict]) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the EventSource instrumentation probe")
    init_script = runpy.run_path(str(SCRIPT))["INIT_SCRIPT"]
    source = f"""
class FakeEventSource {{
  static CLOSED = 2;
  constructor() {{ this.readyState = 1; this.listeners = {{}}; }}
  addEventListener(kind, callback) {{ (this.listeners[kind] ||= []).push(callback); }}
  emit(kind, event) {{ for (const callback of this.listeners[kind] || []) callback(event); }}
  close() {{ this.readyState = FakeEventSource.CLOSED; }}
}}
global.performance = {{now: () => 1}};
global.window = {{EventSource: FakeEventSource, fetch: async () => undefined, addEventListener: () => undefined}};
eval({json.dumps(init_script)});
const source = new window.EventSource('/api/v1/stream');
for (const item of {json.dumps(events)}) {{
  const event = {{lastEventId: item.id, data: JSON.stringify(item.body)}};
  for (const kind of item.types) source.emit(kind, event);
}}
const state = window.__horizonStream;
console.log(JSON.stringify({{
  ids: state.ids, sequences: state.sequences, generations: state.generations,
  duplicates: state.duplicates, bytes: state.bytes,
  sequencesOrdered: state.sequencesOrdered,
  generationsOrdered: state.generationsOrdered,
  retentionOverflow: state.retentionOverflow
}}));
"""
    result = subprocess.run([node, "-e", source], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_browser_harness_sets_hidden_before_dispatch_and_counts_eventsource_data():
    text = SCRIPT.read_text(encoding="utf-8")
    hidden = text.index("visibilityState")
    dispatch = text.index("visibilitychange")
    assert hidden < dispatch
    assert "new TextEncoder().encode" in text
    assert "window.__horizonStream.errors" in text
    assert "(() => {" in text and "})();" in text
    assert '"streamCloses": int' in text
    assert '"reconnects": int' in text


def test_browser_harness_binds_generation_before_render_observation():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "body.generation" in text
    assert "horizon:status-applied" in text
    assert "event.detail?.generation" in text
    assert '"status_ui_latency_ms"' in text


def test_browser_harness_records_named_status_with_event_id_sequence_fallback():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "const recordEvent = event =>" in text
    assert "this.addEventListener('message', recordEvent);" in text
    assert "this.addEventListener('status', recordEvent);" in text
    assert "const canonicalEventId = raw =>" in text
    assert "bodySequence" in text and "eventId" in text
    assert "body.generation remains the convergence generation" in text


def test_browser_harness_shared_recorder_is_identity_deduplicated():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "const recordedEvents = new WeakSet();" in text
    assert "recordedEvents.has(event)" in text
    assert "recordedEvents.add(event)" in text
    # Duplicate accounting occurs once after id and sequence checks, rather
    # than once per listener/field for the same Event.
    assert text.count("if (duplicate) state.duplicates++;" ) == 1


def test_named_status_rejects_noncanonical_or_unsafe_last_event_ids():
    malformed = ["", "01", "+1", " 1 ", "1\n", "1\t", "1.0", "1e2", "-1", "9007199254740992"]
    events = [
        {"id": value, "body": {"generation": index + 1}, "types": ["status"]}
        for index, value in enumerate(["0", "1", "9007199254740991", *malformed])
    ]
    state = _node_stream_probe(events)
    assert state["ids"] == [0, 1, 9007199254740991]
    assert state["sequences"] == [0, 1, 9007199254740991]
    assert len(state["generations"]) == len(events)
    assert not state["retentionOverflow"]


def test_named_and_generic_same_event_is_recorded_once_and_duplicate_once():
    state = _node_stream_probe([
        {"id": "7", "body": {"generation": 5}, "types": ["status", "message"]},
        {"id": "7", "body": {"generation": 5}, "types": ["status"]},
    ])
    assert state["ids"] == [7]
    assert state["sequences"] == [7]
    assert state["generations"] == [5]
    assert state["duplicates"] == 1
    assert state["bytes"] == 32


def test_stream_evidence_retention_overflow_is_bounded_and_fail_closed():
    events = [
        {"id": str(index), "body": {"generation": index}, "types": ["status"]}
        for index in range(1, 1026)
    ]
    state = _node_stream_probe(events)
    assert len(state["ids"]) == 1024
    assert len(state["sequences"]) == 1024
    assert len(state["generations"]) == 1024
    assert state["retentionOverflow"] is True
    text = SCRIPT.read_text(encoding="utf-8")
    assert "const MAX_STREAM_EVENTS = 1024;" in text
    assert 'raise SystemExit("stream evidence retention exceeded MAX_STREAM_EVENTS")' in text


def test_browser_harness_v2_has_bounded_hidden_and_reconnect_measurements():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'phase2.1.browser.v2' in text
    assert 'hidden_request_grace_s' in text
    assert 'status_calls_while_stream_healthy' in text
    assert 'forced_reconnect_observed' in text
    assert 'unique_stream_sequences' in text
    assert '__horizonForceStreamFailure' in text
    assert 'resume_authoritative_status_requests' in text
    assert 'resume_stream_opens' in text
    assert 'stream_sequences_ordered' in text
    assert 'stream_generations_non_decreasing' in text


def test_watch_fixture_is_identity_free_and_bounded():
    script = SCRIPT.parents[1] / "ops/bin/horizon-phase2-live-acceptance"
    text = script.read_text(encoding="utf-8")
    assert 'phase2.live.fixture.v1' in text
    assert 'no arbitrary' in text.lower()
    assert '"identityFree": True' in text
    assert 'queueOverflow' in text and 'slowSubscriberEvicted' in text
    assert 'written to stdout only' in text
    assert '--output' not in text


def test_watch_fixture_rejects_non_monotonic_order_contract():
    script = SCRIPT.parents[1] / "ops/bin/horizon-phase2-live-acceptance"
    import runpy
    module = runpy.run_path(str(script))
    assert module["_strictly_increasing"]([1, 2, 3, 5, 6, 7])
    assert not module["_strictly_increasing"]([1, 2, 3, 5, 6, 7, 4])
    assert module["_order_contract"]([(1, 1), (2, 2), (3, 3), (5, 4), (6, 5), (7, 6)])
    assert not module["_order_contract"]([(1, 1), (2, 2), (3, 3), (5, 4), (6, 5), (7, 6), (4, 7)])
    assert not module["_order_contract"]([(1, 1), (2, 2), (3, 1)])


def test_watch_fixture_rejects_late_slow_item_in_causal_aggregate():
    script = SCRIPT.parents[1] / "ops/bin/horizon-phase2-live-acceptance"
    import runpy
    import asyncio
    module = runpy.run_path(str(script))
    report = asyncio.run(module["_run"]())
    assert report["pass"]
    assert report["aggregateOrderContract"]
    assert report["crossSubscriberTotalOrderClaimed"] is False
    assert report["crossSubscriberGenerationOrder"]["healthy"]
    assert report["crossSubscriberGenerationOrder"]["slow"]
    late = [("healthy", 1, 1), ("healthy", 2, 2), ("healthy", 3, 3),
            ("healthy", 5, 4), ("healthy", 6, 5), ("healthy", 7, 6),
            ("slow", 4, 7)]
    assert not module["_causal_aggregate_contract"](late)


def test_browser_harness_correlates_fallback_to_stream_health_and_bounds_force_window():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "healthyConnections" in text
    assert "healthyStatusCalls" in text and "degradedStatusCalls" in text
    assert "reconnect_after_force" in text
    force_capture = text.index("reconnect_after_force")
    hide = text.index("visibilityState', {value: 'hidden")
    assert force_capture < hide


def test_browser_harness_forces_idle_active_eventsource_not_transport_timeout():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "activeSources" in text and "__horizonForceStreamFailure" in text
    assert "item.__horizonHealthy" in text
    assert "source.close();" in text and "source.dispatchEvent(new Event('error'))" in text
    # An idle socket must not be credited merely because a short offline
    # interval elapsed; the helper targets a known healthy stream directly.
    assert "set_offline(True)" not in text


def test_forced_reconnect_snapshot_cannot_borrow_visibility_resume_credit():
    import runpy

    module = runpy.run_path(str(SCRIPT))
    observed = module["_forced_reconnect_observed"]
    before = {"failures": 0, "errors": 0, "reconnects": 0, "opens": 1}
    assert observed(forced=True, before=before, snapshot={"failures": 1, "errors": 1, "reconnects": 1, "opens": 2})
    # A visibility-only second open cannot satisfy the frozen force window.
    assert not observed(forced=False, before=before, snapshot={"failures": 0, "errors": 0, "reconnects": 0, "opens": 2})
    assert not observed(forced=True, before=before, snapshot={"failures": 0, "errors": 0, "reconnects": 0, "opens": 2})


def test_forced_reconnect_waits_for_error_and_new_open_before_hidden():
    text = SCRIPT.read_text(encoding="utf-8")
    force = text.index("__horizonForceStreamFailure?.()")
    wait = text.index("forcedFailures > failures")
    hide = text.index("visibilityState', {value: 'hidden")
    assert force < wait < hide
    assert "force_snapshot =" in text
    assert "Freeze force evidence before hidden/resume" in text


def test_error_dispatch_without_replacement_open_cannot_claim_reconnect():
    import runpy

    module = runpy.run_path(str(SCRIPT))
    observed = module["_forced_reconnect_observed"]
    before = {"failures": 0, "errors": 0, "reconnects": 0, "opens": 1}
    # Adversarial harness outcome: forced close/error happened, but no
    # replacement EventSource opened.  Error dispatch alone is insufficient.
    assert not observed(
        forced=True,
        before=before,
        snapshot={"failures": 1, "errors": 1, "reconnects": 0, "opens": 1},
    )
    text = SCRIPT.read_text(encoding="utf-8")
    assert "state.reconnects++;" in text
    assert text.index("state.reconnects++;") > text.index("this.addEventListener('open'")
    assert "this.__horizonReplacement = state.awaitingReplacement > 0;" in text
    assert "this.__horizonReplacement && state.awaitingReplacement > 0" in text
    assert "arguments[" not in text
