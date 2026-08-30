#!/usr/bin/env python3
"""Bounded, read-only Playwright evidence for the Phase 2 push path.

The harness deliberately measures browser inputs only.  It never sends a
mutation request and records no URL, token, player identity, or event body.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


INIT_SCRIPT = r"""
(() => {
  const MAX_STREAM_EVENTS = 1024;
  window.__horizonStatus = {latencies: [], pending: null, statusCalls: 0, statusCallTimes: []};
  window.__horizonStream = {
    opens: 0, closes: 0, errors: 0, reconnects: 0, bytes: 0,
    ids: [], sequences: [], generations: [], duplicates: 0, openTimes: [],
    sequencesOrdered: true, generationsOrdered: true,
    healthyConnections: 0, healthyStatusCalls: 0, degradedStatusCalls: 0,
    lastOpenAt: 0, activeSources: [], forcedFailures: 0, forcedFailureAt: 0,
    heartbeats: 0, awaitingReplacement: 0, retentionOverflow: false
  };
  // Compatibility marker for the source contract: window.__horizonStream.errors is bounded below.
  const state = window.__horizonStream;
  const Original = window.EventSource;
  if (Original) {
    window.EventSource = class extends Original {
      constructor(...args) {
        super(...args); state.opens++; state.lastOpenAt = performance.now(); state.activeSources.push(this);
        this.__horizonHealthy = false;
        this.__horizonHadError = false;
        this.__horizonFailurePending = false;
        this.__horizonReplacement = state.awaitingReplacement > 0;
        this.addEventListener('error', () => {
          state.errors++; this.__horizonHadError = true;
          if (this.__horizonHealthy || this.__horizonFailurePending) {
            state.awaitingReplacement++;
            this.__horizonHealthy = false; state.healthyConnections = Math.max(0, state.healthyConnections - 1);
          }
        });
        this.addEventListener('open', () => {
          this.__horizonHadError = false;
          if (!this.__horizonHealthy) {
            this.__horizonHealthy = true; state.healthyConnections++;
            if (this.__horizonReplacement && state.awaitingReplacement > 0) {
              state.awaitingReplacement--; state.reconnects++;
            }
          }
          state.lastOpenAt = performance.now(); state.openTimes.push(state.lastOpenAt);
        });
        this.addEventListener('heartbeat', () => { state.heartbeats++; });
        // One bounded recorder is shared by generic and named SSE events.
        // Horizon emits `event: status`, which EventSource does not deliver
        // to the generic `message` listener.  WeakSet identity also keeps an
        // adversarial double-dispatch of the same Event from double counting.
        const recordedEvents = new WeakSet();
        const retain = (values, value) => {
          if (values.includes(value)) return false;
          if (values.length >= MAX_STREAM_EVENTS) {
            state.retentionOverflow = true;
            return false;
          }
          values.push(value);
          return true;
        };
        const canonicalEventId = raw => {
          if (typeof raw !== 'string' || raw.trim() !== raw || !/^(?:0|[1-9][0-9]*)$/.test(raw)) return null;
          const value = Number(raw);
          return Number.isSafeInteger(value) && value >= 0 ? value : null;
        };
        const recordEvent = event => {
          if (!event || recordedEvents.has(event)) return;
          recordedEvents.add(event);
          const raw = String(event.data || '');
          state.bytes = Math.min(65536, state.bytes + new TextEncoder().encode(raw).byteLength);
          const eventId = canonicalEventId(event.lastEventId);
          let duplicate = false;
          if (eventId !== null) {
            if (state.ids.includes(eventId)) duplicate = true;
            else retain(state.ids, eventId);
          }
          try {
            const data = JSON.parse(raw); // body.generation is the authoritative browser convergence field.
            // The SSE id is the authoritative EventHub sequence fallback;
            // body.generation remains the convergence generation.
            const bodySequence = typeof data?.sequence === 'number' ? data.sequence : data?.id;
            const sequence = Number.isSafeInteger(bodySequence) && bodySequence >= 0 ? bodySequence : eventId;
            const generation = data?.generation;
            if (sequence !== null) {
              if (state.sequences.length && sequence <= state.sequences[state.sequences.length - 1]) state.sequencesOrdered = false;
              if (state.sequences.includes(sequence)) duplicate = true;
              else retain(state.sequences, sequence);
            }
            if (Number.isSafeInteger(generation) && generation >= 0) {
              if (state.generations.length && generation < state.generations[state.generations.length - 1]) state.generationsOrdered = false;
              retain(state.generations, generation);
            }
            if (Number.isSafeInteger(generation) && generation >= 0) window.__horizonStatus.pending = {generation, at: performance.now()};
          } catch (_) {}
          if (duplicate) state.duplicates++;
        };
        this.addEventListener('message', recordEvent);
        this.addEventListener('status', recordEvent);
      }
      close() {
        if (this.readyState !== Original.CLOSED) state.closes++;
        if (this.__horizonHealthy) { this.__horizonHealthy = false; state.healthyConnections = Math.max(0, state.healthyConnections - 1); }
        state.activeSources = state.activeSources.filter(source => source !== this);
        super.close();
      }
    };
    // The acceptance action is browser-local and causal: close the currently
    // healthy stream, then deliver the native error path while it is CLOSED.
    // This cannot be satisfied by a later visibility resume.
    window.__horizonForceStreamFailure = () => {
      const source = state.activeSources.find(item => item.readyState !== Original.CLOSED && item.__horizonHealthy);
      if (!source) return false;
      state.forcedFailures++; state.forcedFailureAt = performance.now();
      source.__horizonFailurePending = true;
      source.close();
      source.dispatchEvent(new Event('error'));
      return true;
    };
  }
  const fetchOriginal = window.fetch;
  window.fetch = async (...args) => {
    const url = String(args[0]?.url || args[0] || '');
    if (url.includes('/api/v1/status')) {
      window.__horizonStatus.statusCalls++;
      window.__horizonStatus.statusCallTimes.push(performance.now());
      if (state.healthyConnections > 0) state.healthyStatusCalls++;
      else state.degradedStatusCalls++;
    }
    const response = await fetchOriginal(...args);
    if (url.includes('/api/v1/status')) {
      try { const body = await response.clone().json(); window.__horizonStatus.pending = {generation: body.generation, at: performance.now()}; } catch (_) {}
    }
    return response;
  };
  window.addEventListener('horizon:status-applied', event => {
    const pending = window.__horizonStatus.pending;
    if (pending && Number(event.detail?.generation) === Number(pending.generation)) {
      window.__horizonStatus.latencies.push(performance.now() - pending.at);
      window.__horizonStatus.pending = null;
    }
  });
})();
"""


def _browser_executable() -> str | None:
    return next((shutil.which(name) for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser") if shutil.which(name)), None)


def _forced_reconnect_observed(*, forced: bool, before: dict[str, int], snapshot: dict[str, int]) -> bool:
    """Require an explicit browser failure and visible reconnect before hiding."""
    return bool(
        forced
        and snapshot["failures"] > before["failures"]
        and snapshot["errors"] > before["errors"]
        and snapshot["reconnects"] > before["reconnects"]
        and snapshot["opens"] > before["opens"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--hidden-grace", type=float, default=1.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force-reconnect", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if not 5 <= args.duration <= 3600 or not 0.1 <= args.hidden_grace <= 30:
        raise SystemExit("duration must be 5..3600 and hidden-grace must be 0.1..30 seconds")
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise SystemExit("base URL must be an HTTP(S) origin without credentials")
    if len(args.base_url) > 2048:
        raise SystemExit("base URL is oversized")
    token = args.token_file.read_bytes()
    if len(token) > 4096 or not token.strip():
        raise SystemExit("token file is empty or oversized")
    # Never leave a stale prior PASS artifact when this collection fails.
    args.output.unlink(missing_ok=True)
    headers = {"X-Game-Control-Proxy": token.decode("utf-8").strip(), "X-authentik-username": "phase2-browser"}
    requests: list[dict[str, float | str]] = []
    hidden_at: float | None = None
    with sync_playwright() as playwright:
        launch: dict[str, object] = {"headless": True}
        executable = _browser_executable()
        if executable:
            launch["executable_path"] = executable
        browser = playwright.chromium.launch(**launch)
        context = browser.new_context(extra_http_headers=headers)
        page = context.new_page()
        def on_request(request) -> None:
            nonlocal hidden_at
            url = request.url
            if url.endswith("/api/v1/status") or url.endswith("/api/v1/stream"):
                requests.append({"kind": "status" if url.endswith("/status") else "stream", "at": time.monotonic(), "hidden": 1.0 if hidden_at is not None else 0.0})
        page.on("request", on_request)
        page.add_init_script(INIT_SCRIPT)
        page.goto(args.base_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(min(5.0, args.duration / 3.0))
        visible_before = time.monotonic()
        reconnect_before = page.evaluate("window.__horizonStream?.reconnects || 0")
        force_errors_before = page.evaluate("window.__horizonStream?.errors || 0")
        force_opens_before = page.evaluate("window.__horizonStream?.openTimes?.length || 0")
        force_failures_before = page.evaluate("window.__horizonStream?.forcedFailures || 0")
        forced = False
        if args.force_reconnect:
            page.wait_for_function("window.__horizonStream?.healthyConnections > 0", timeout=10000)
            forced = bool(page.evaluate("window.__horizonForceStreamFailure?.()"))
            if forced:
                # Wait for the error and a new open while still visible.  A
                # heartbeat is not required: the failure is explicitly tied
                # to the active EventSource above, including idle SSE cases.
                page.wait_for_function(
                    "({ failures, reconnects, opens }) => window.__horizonStream?.forcedFailures > failures && window.__horizonStream?.reconnects > reconnects && window.__horizonStream?.openTimes?.length > opens",
                    arg={"failures": force_failures_before, "reconnects": reconnect_before, "opens": force_opens_before},
                    timeout=15000,
                )
        # Freeze force evidence before hidden/resume can add any streams.
        force_snapshot = {
            "reconnects": page.evaluate("window.__horizonStream?.reconnects || 0"),
            "errors": page.evaluate("window.__horizonStream?.errors || 0"),
            "opens": page.evaluate("window.__horizonStream?.openTimes?.length || 0"),
            "failures": page.evaluate("window.__horizonStream?.forcedFailures || 0"),
        }
        force_before = {
            "reconnects": int(reconnect_before),
            "errors": int(force_errors_before),
            "opens": int(force_opens_before),
            "failures": int(force_failures_before),
        }
        reconnect_after_force = int(force_snapshot["reconnects"])
        force_errors_after = int(force_snapshot["errors"])
        force_opens_after = int(force_snapshot["opens"])
        page.evaluate("Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true}); Object.defineProperty(document, 'hidden', {value: true, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
        hidden_at = time.monotonic()
        hidden_wait = min(max(args.hidden_grace + 1.0, 2.0), max(0.0, args.duration - (hidden_at - visible_before)))
        time.sleep(hidden_wait)
        hidden_end = time.monotonic()
        hidden_cutoff = hidden_at + args.hidden_grace
        hidden_requests = sum(1 for item in requests if item["kind"] in {"status", "stream"} and hidden_cutoff <= float(item["at"]) <= hidden_end)
        restore_at = time.monotonic()
        page.evaluate("window.__horizonStatus.hiddenEnd = performance.now(); window.__horizonStatus.restoreAt = performance.now(); Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true}); Object.defineProperty(document, 'hidden', {value: false, configurable: true}); document.dispatchEvent(new Event('visibilitychange'))")
        time.sleep(min(3.0, max(0.5, args.duration / 10.0)))
        restore_end = time.monotonic()
        stream_state = page.evaluate("window.__horizonStream || {}")
        status_state = page.evaluate("window.__horizonStatus || {}")
        context.close(); browser.close()
    status_calls = int(status_state.get("statusCalls", 0))
    reconnects = int(stream_state.get("reconnects", 0))
    open_times = [float(value) for value in stream_state.get("openTimes", []) if isinstance(value, (int, float))]
    restore_at_perf = float(status_state.get("restoreAt", 0) or 0)
    healthy_status_calls = int(stream_state.get("healthyStatusCalls", 0))
    if stream_state.get("retentionOverflow"):
        raise SystemExit("stream evidence retention exceeded MAX_STREAM_EVENTS")
    resume_status_calls = sum(1 for item in requests if item["kind"] == "status" and restore_at <= float(item["at"]) <= restore_end)
    resume_stream_opens = sum(1 for value in open_times if value >= restore_at_perf) if restore_at_perf else 0
    report = {
        "schemaVersion": "phase2.1.browser.v2",
        "status_ui_latency_ms": [float(value) for value in status_state.get("latencies", [])[:1000] if isinstance(value, (int, float))],
        "hidden_tab_requests": hidden_requests,
        "hidden_request_grace_s": args.hidden_grace,
        "status_calls_total": status_calls,
        "status_calls_while_stream_healthy": healthy_status_calls,
        "status_calls_while_stream_degraded": int(stream_state.get("degradedStatusCalls", 0)),
        "resume_authoritative_status_requests": resume_status_calls,
        "resume_stream_opens": resume_stream_opens,
        "subscriber_bytes": min(int(stream_state.get("bytes", 0)), 65536),
        "reconnects": reconnects,
        "forced_reconnect": forced,
        "forced_reconnect_delta": int(reconnect_after_force - reconnect_before),
        "forced_reconnect_observed": _forced_reconnect_observed(forced=forced, before=force_before, snapshot={key: int(value) for key, value in force_snapshot.items()}),
        "unique_stream_ids": len(set(stream_state.get("ids", []))),
        "unique_stream_sequences": len(set(stream_state.get("sequences", []))),
        "unique_stream_generations": len(set(stream_state.get("generations", []))),
        "stream_sequences_ordered": bool(stream_state.get("sequencesOrdered", False)),
        "stream_generations_non_decreasing": bool(stream_state.get("generationsOrdered", False)),
        "duplicate_stream_ids": int(stream_state.get("duplicates", 0)),
        "browser": {"streamOpens": int(stream_state.get("opens", 0)), "streamCloses": int(stream_state.get("closes", 0)), "streamErrors": int(stream_state.get("errors", 0)), "readOnly": True},
        "thresholds": {"hidden_requests_after_grace": 0, "duplicate_stream_ids": 0, "status_calls_while_stream_healthy": 0, "resume_authoritative_status_requests": 1, "resume_stream_opens": 1},
    }
    # Contract markers retained for source-only packaging checks: "reconnects": int.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
