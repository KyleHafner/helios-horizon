#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BASE_URL:-http://192.0.2.10:8444}
ACTOR=${ACTOR:-codex}
SAMPLES=${SAMPLES:-50}
OUTPUT=${OUTPUT:-docs/perf/2026-07-15-baseline.md}
PYTHON_BIN=${PYTHON_BIN:-.venv/bin/python}
PROXY_TOKEN=${PROXY_TOKEN:?set PROXY_TOKEN to the live game-control proxy credential}

command -v curl >/dev/null
command -v jq >/dev/null
test -x "$PYTHON_BIN"
mkdir -p "$(dirname "$OUTPUT")"
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

headers=(
  -H "X-Game-Control-Proxy: $PROXY_TOKEN"
  -H "X-authentik-username: $ACTOR"
)

measure_target() {
  local name=$1 path=$2
  local output="$tmpdir/$name.seconds"
  : >"$output"
  for ((i = 1; i <= SAMPLES; i++)); do
    curl --fail --silent --show-error "${headers[@]}" \
      -o /dev/null -w '%{time_total}\n' "$BASE_URL$path" >>"$output"
  done
}

summarize_target() {
  local name=$1 path=$2
  awk '
    { values[NR] = $1 * 1000; sum += values[NR] }
    END {
      n = NR
      for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) if (values[j] < values[i]) { t = values[i]; values[i] = values[j]; values[j] = t }
      p50 = values[int((n - 1) * 0.50) + 1]
      p95 = values[int((n - 1) * 0.95) + 1]
      printf "| %s | %s | %d | %.2f | %.2f | %.2f | %.2f |\n", "'"$name"'", "'"$path"'", n, sum / n, p50, p95, values[n]
    }
  ' "$tmpdir/$name.seconds"
}

measure_target status /api/v1/status
measure_target profiles /api/v1/profiles
measure_target app /app.js
measure_target styles /styles.css

curl --fail --silent --show-error "${headers[@]}" "$BASE_URL/api/v1/perf" >"$tmpdir/perf.json"

tti=$(
  BASE_URL="$BASE_URL" PROXY_TOKEN="$PROXY_TOKEN" ACTOR="$ACTOR" "$PYTHON_BIN" - <<'PY'
import os
import shutil
import time

from playwright.sync_api import sync_playwright

base_url = os.environ["BASE_URL"]
headers = {
    "X-Game-Control-Proxy": os.environ["PROXY_TOKEN"],
    "X-authentik-username": os.environ["ACTOR"],
}
with sync_playwright() as playwright:
    executable = next(
        (shutil.which(name) for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser") if shutil.which(name)),
        None,
    )
    launch = {"headless": True}
    if executable:
        launch["executable_path"] = executable
    browser = playwright.chromium.launch(**launch)
    context = browser.new_context(extra_http_headers=headers)
    page = context.new_page()
    started = time.perf_counter()
    page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_selector('[data-profile-id]', timeout=30000)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    mark_ms = page.evaluate("""() => {
        const entries = performance.getEntriesByName('horizon-load-to-first-status-paint', 'measure');
        return entries.length ? entries.at(-1).duration : null;
    }""")
    print(f"{elapsed_ms:.2f}|{'' if mark_ms is None else f'{mark_ms:.2f}'}")
    context.close()
    browser.close()
PY
)
tti_navigation_ms=${tti%%|*}
tti_mark_ms=${tti#*|}

{
  echo "# Horizon Wave 1 baseline — 2026-07-15"
  echo
  echo "- Target: \`$BASE_URL\`"
  echo "- Captured: \`$(date -u +%Y-%m-%dT%H:%M:%SZ)\`"
  echo "- Samples: \`$SAMPLES\` per HTTP target"
  echo "- Running-profile safety: measurements are read-only; no game profile lifecycle action is performed."
  echo
  echo "## HTTP timings"
  echo
  echo "Times are milliseconds from curl's \`time_total\`; p50/p95 use nearest-rank order statistics."
  echo
  echo "| Target | Path | Count | Mean ms | P50 ms | P95 ms | Max ms |"
  echo "| --- | --- | ---: | ---: | ---: | ---: | ---: |"
  summarize_target status /api/v1/status
  summarize_target profiles /api/v1/profiles
  summarize_target app /app.js
  summarize_target styles /styles.css
  echo
  echo "## Headless Playwright TTI"
  echo
  echo "- Navigation to first rendered profile card: \`${tti_navigation_ms} ms\`"
  echo "- App mark \`horizon-load-to-first-status-paint\`: \`${tti_mark_ms:-not recorded} ms\`"
  echo
  echo "## Wave 1 snapshot"
  echo
  echo '```json'
  jq . "$tmpdir/perf.json"
  echo '```'
} >"$OUTPUT"

echo "wrote $OUTPUT"
