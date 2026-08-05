import sqlite3

import pytest

from game_control.tps import TpsSampler, parse_metrics


SAMPLE = """
# HELP mc_server_tick_seconds Stats on server tick durations
# TYPE mc_server_tick_seconds summary
mc_server_tick_seconds{quantile="0.5"} 0.02513
mc_server_tick_seconds_count 5000
"""

LIVE_HISTOGRAM = """
# HELP mc_server_tick_seconds Stats on server tick times.
# TYPE mc_server_tick_seconds histogram
mc_server_tick_seconds_bucket{le="0.025"} 50.0
mc_server_tick_seconds_bucket{le="0.05"} 100.0
mc_server_tick_seconds_bucket{le="+Inf"} 100.0
mc_server_tick_seconds_count 100.0
mc_server_tick_seconds_sum 2.513
"""


def test_parse_median_tick_to_tps_and_mspt():
    tps, mspt = parse_metrics(SAMPLE)
    assert round(mspt, 2) == 25.13
    assert round(tps, 2) == 20.0


def test_parse_slow_ticks():
    slow = SAMPLE.replace("0.02513", "0.100")
    tps, mspt = parse_metrics(slow)
    assert round(tps, 1) == 10.0
    assert round(mspt, 1) == 100.0


def test_parse_live_exporter_histogram_family():
    tps, mspt = parse_metrics(LIVE_HISTOGRAM)
    assert round(mspt, 2) == 25.13
    assert round(tps, 2) == 20.0


def test_parse_garbage_returns_none():
    assert parse_metrics("<html>nope</html>") is None
    assert parse_metrics("") is None


def test_tps_sampler_is_minecraft_only():
    with pytest.raises(ValueError, match="Minecraft"):
        TpsSampler(sqlite3.connect(":memory:"), profile_id="pz-rising")
