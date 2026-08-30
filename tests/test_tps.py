import asyncio
import sqlite3
from datetime import datetime

import httpx
import pytest

import game_control.tps as tps_module
from game_control.tps import MAX_EXPORTER_RESPONSE_BYTES, TpsSampler, parse_metrics


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


class FakeResponse:
    def __init__(self, text: str, error: Exception | None = None, chunks: list[bytes] | None = None):
        self.body = text.encode()
        self.error = error
        self.chunks = chunks

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    async def aiter_bytes(self):
        for chunk in self.chunks or [self.body]:
            yield chunk


class FakeStream:
    def __init__(self, response: FakeResponse | None, error: Exception | None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *_args):
        return None


class FakeClient:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.closed = False

    def stream(self, _method: str, _url: str):
        return FakeStream(self.response, self.error)

    async def aclose(self):
        self.closed = True


def make_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE metric_samples (profile_id TEXT, metric TEXT, ts TEXT, value REAL)"
    )
    return connection


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


def test_parse_metrics_rejects_non_text_and_hostile_samples():
    assert parse_metrics(None) is None
    assert parse_metrics(123) is None
    hostile = """
    mc_server_tick_seconds{quantile="0.5"} NaN
    mc_server_tick_seconds_count Inf
    mc_server_tick_seconds_sum -Inf
    mc_server_tick_seconds_bucket{le="not-a-number"} 3
    mc_server_tick_seconds{quantile="0.5"
    this is not a Prometheus sample
    """
    assert parse_metrics(hostile) is None


def test_parse_metrics_uses_sum_count_when_histogram_has_no_buckets():
    text = """
    mc_server_tick_seconds_sum 2
    mc_server_tick_seconds_count 4
    """
    assert parse_metrics(text) == pytest.approx((2.0, 500.0))


def test_parse_metrics_uses_sorted_median_bucket_and_ignores_plus_inf():
    text = """
    mc_server_tick_seconds_bucket{le="0.3"} 10
    mc_server_tick_seconds_bucket{le="0.1"} 2
    mc_server_tick_seconds_bucket{le="0.2"} 5
    mc_server_tick_seconds_bucket{le="+Inf"} 10
    """
    assert parse_metrics(text) == pytest.approx((5.0, 200.0))


def test_parse_metrics_handles_zero_negative_and_absurd_tick_bounds():
    assert parse_metrics('mc_server_tick_seconds{quantile="0.5"} 0') is None
    assert parse_metrics('mc_server_tick_seconds{quantile="0.5"} -1') is None
    assert parse_metrics('mc_server_tick_seconds{quantile="0.5"} 1e-12') == pytest.approx((20.0, 1e-9))
    assert parse_metrics('mc_server_tick_seconds{quantile="0.5"} 1e12') == pytest.approx((1e-12, 1e15))


def test_tps_sampler_is_minecraft_only():
    connection = make_connection()
    try:
        with pytest.raises(ValueError, match="Minecraft"):
            TpsSampler(connection, profile_id="pz-rising")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_tps_sampler_clamps_intervals_and_closes_owned_client():
    connection = make_connection()
    external = FakeClient()
    sampler = TpsSampler(connection, interval_seconds=0.1, backoff_seconds=0.2, client=external)
    assert sampler.interval_seconds == 1.0
    assert sampler.backoff_seconds == 1.0
    await sampler.aclose()
    assert external.closed is False

    owned = TpsSampler(connection)
    assert owned._owns_client is True
    await owned.aclose()
    connection.close()


@pytest.mark.asyncio
async def test_run_once_persists_tps_and_mspt_with_explicit_timestamp():
    connection = make_connection()
    sampler = TpsSampler(connection, client=FakeClient(FakeResponse(SAMPLE)))
    assert await sampler.run_once(now="2026-07-16T13:00:00Z") is True
    rows = connection.execute("SELECT metric, ts, value FROM metric_samples ORDER BY metric").fetchall()
    assert rows == [
        ("mspt", "2026-07-16T13:00:00Z", pytest.approx(25.13)),
        ("tps", "2026-07-16T13:00:00Z", pytest.approx(20.0)),
    ]
    connection.close()


@pytest.mark.asyncio
async def test_run_once_normalizes_naive_clock_timestamp():
    connection = make_connection()
    sampler = TpsSampler(
        connection,
        client=FakeClient(FakeResponse(SAMPLE)),
        clock=lambda: datetime(2026, 7, 16, 13, 0, 0),
    )
    assert await sampler.run_once() is True
    assert connection.execute("SELECT DISTINCT ts FROM metric_samples").fetchone()[0] == "2026-07-16T13:00:00Z"
    connection.close()


@pytest.mark.asyncio
async def test_run_once_rejects_exporter_response_over_streaming_cap():
    connection = make_connection()
    oversized = FakeResponse(
        "",
        chunks=[b"x" * (MAX_EXPORTER_RESPONSE_BYTES + 1)],
    )
    sampler = TpsSampler(connection, client=FakeClient(oversized))
    assert await sampler.run_once() is False
    assert connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 0
    connection.close()


@pytest.mark.asyncio
async def test_run_once_rejects_cumulative_exporter_chunks_over_cap():
    connection = make_connection()
    oversized = FakeResponse(
        "",
        chunks=[b"x" * (MAX_EXPORTER_RESPONSE_BYTES - 1), b"y" * 2],
    )
    sampler = TpsSampler(connection, client=FakeClient(oversized))
    assert await sampler.run_once() is False
    assert connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 0
    connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadTimeout("timed out"), OSError("offline")])
async def test_run_once_skips_missing_or_unreachable_exporter_data(failure):
    connection = make_connection()
    client = FakeClient(error=failure)
    sampler = TpsSampler(connection, client=client)
    assert await sampler.run_once() is False
    assert connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 0

    missing = TpsSampler(connection, client=FakeClient(FakeResponse("# no telemetry")))
    assert await missing.run_once() is False
    assert connection.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0] == 0
    connection.close()


@pytest.mark.asyncio
async def test_run_uses_backoff_after_three_failures_and_resets_on_success(monkeypatch):
    connection = make_connection()
    sampler = TpsSampler(connection, interval_seconds=2, backoff_seconds=7, client=FakeClient())
    outcomes = iter([False, False, False, True])
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError

    async def is_running():
        return True

    async def fake_run_once():
        return next(outcomes)

    monkeypatch.setattr(tps_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sampler, "run_once", fake_run_once)
    with pytest.raises(asyncio.CancelledError):
        await sampler.run(is_running)
    assert delays == [2.0, 2.0, 7.0, 2.0]
    assert sampler.consecutive_failures == 0
    connection.close()


@pytest.mark.asyncio
async def test_run_treats_running_check_errors_as_stopped(monkeypatch):
    connection = make_connection()
    sampler = TpsSampler(connection, interval_seconds=3, client=FakeClient())
    delays = []

    def is_running():
        raise RuntimeError("profile disappeared")

    async def fake_sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(tps_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await sampler.run(is_running)
    assert delays == [3.0]
    assert sampler.consecutive_failures == 0
    connection.close()
