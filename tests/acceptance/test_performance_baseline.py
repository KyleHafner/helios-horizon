from pathlib import Path


def test_performance_baseline_script_is_live_safe_and_covers_targets():
    script = Path(__file__).resolve().parents[2] / "tools/acceptance/performance-baseline.sh"
    text = script.read_text()

    assert "SAMPLES=${SAMPLES:-50}" in text
    assert "/api/v1/status" in text
    assert "/api/v1/profiles" in text
    assert "/app.js" in text
    assert "/styles.css" in text
    assert "playwright" in text
    assert "docs/perf/2026-07-15-baseline.md" in text
    assert "PROXY_TOKEN" in text
