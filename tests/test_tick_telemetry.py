from __future__ import annotations

import pytest

from game_control.tick_telemetry import (
    ExporterRegistry,
    ExporterSpec,
    MAX_HISTOGRAMS_PER_PROFILE,
    MAX_PROFILES,
    PrometheusTickParser,
    TickTelemetry,
    TickSnapshot,
)


def exposition(values: tuple[int, int, int, int], *, labels: str = '') -> str:
    b1, b2, b3, inf = values
    suffix = ('{' + labels + '}' if labels else '')
    return '\n'.join(
        (
            f'mc_server_tick_seconds_bucket{{le="0.01"{"," if labels else ""}{labels}}} {b1}',
            f'mc_server_tick_seconds_bucket{{le="0.05"{"," if labels else ""}{labels}}} {b2}',
            f'mc_server_tick_seconds_bucket{{le="0.1"{"," if labels else ""}{labels}}} {b3}',
            f'mc_server_tick_seconds_bucket{{le="+Inf"{"," if labels else ""}{labels}}} {inf}',
            f'mc_server_tick_seconds_sum{suffix} {inf / 100}',
            f'mc_server_tick_seconds_count{suffix} {inf}',
        )
    )


def collector() -> TickTelemetry:
    parser = PrometheusTickParser()
    return TickTelemetry(ExporterRegistry({'minecraft': ExporterSpec('minecraft', 'http://127.0.0.1/metrics', parser)}))


def test_histogram_uses_scrape_delta_and_returns_percentiles() -> None:
    telemetry = collector()
    assert telemetry.scrape('minecraft', exposition((1, 2, 3, 4))).reason == 'warmup'
    result = telemetry.scrape('minecraft', exposition((2, 4, 6, 8)))
    assert result.state == 'available'
    assert result.count == 4
    assert result.p50_ms is not None and result.p95_ms is not None and result.p99_ms is not None
    assert result.p50_ms <= result.p95_ms <= result.p99_ms


def test_counter_regression_resets_previous_window() -> None:
    telemetry = collector()
    telemetry.scrape('minecraft', exposition((10, 10, 10, 10)))
    result = telemetry.scrape('minecraft', exposition((1, 2, 3, 4)))
    assert (result.state, result.reason) == ('reset', 'counter_regression')
    assert telemetry.scrape('minecraft', exposition((2, 3, 4, 5))).state == 'available'


def test_bucket_boundary_change_resets_even_when_counters_increase() -> None:
    telemetry = collector()
    telemetry.scrape('minecraft', exposition((1, 2, 3, 4)))
    changed = exposition((2, 3, 4, 5)).replace('le="0.1"', 'le="0.2"')
    result = telemetry.scrape('minecraft', changed)
    assert (result.state, result.reason) == ('reset', 'boundary_changed')


def test_duplicate_samples_and_nonfinite_values_are_unavailable() -> None:
    telemetry = collector()
    duplicate = exposition((1, 2, 3, 4)) + '\nmc_server_tick_seconds_count 4'
    assert telemetry.scrape('minecraft', duplicate).reason == 'invalid_scrape'
    assert telemetry.scrape('minecraft', exposition((1, 2, 3, 4)).replace(' 4\n', ' NaN\n', 1)).reason == 'invalid_scrape'


def test_missing_totals_and_empty_windows_do_not_make_up_percentiles() -> None:
    telemetry = collector()
    missing_first = '\n'.join(line for line in exposition((1, 2, 3, 4)).splitlines() if not line.startswith('mc_server_tick_seconds_sum'))
    telemetry.scrape('minecraft', missing_first)
    missing = '\n'.join(line for line in exposition((2, 3, 4, 5)).splitlines() if not line.startswith('mc_server_tick_seconds_sum'))
    assert telemetry.scrape('minecraft', missing).reason == 'missing_histogram_total'
    telemetry = collector()
    telemetry.scrape('minecraft', exposition((1, 2, 3, 4)))
    assert telemetry.scrape('minecraft', exposition((1, 2, 3, 4))).reason == 'empty_window'


def test_multiple_profiles_have_independent_state_and_registry_is_bounded() -> None:
    parser = PrometheusTickParser()
    registry = ExporterRegistry()
    registry.register(ExporterSpec('minecraft', 'http://127.0.0.1:9101/metrics', parser))
    registry.register(ExporterSpec('terraria', 'http://127.0.0.1:9102/metrics', parser))
    telemetry = TickTelemetry(registry)
    assert telemetry.scrape('minecraft', exposition((1, 2, 3, 4))).reason == 'warmup'
    assert telemetry.scrape('terraria', exposition((1, 2, 3, 4))).reason == 'warmup'
    for index in range(MAX_PROFILES - len(registry.profiles())):
        registry.register(ExporterSpec(f'profile{index}', 'http://127.0.0.1/metrics', parser))
    with pytest.raises(ValueError):
        registry.register(ExporterSpec('overflow', 'http://127.0.0.1/metrics', parser))
    with pytest.raises(ValueError):
        PrometheusTickParser('bad metric')


def test_identity_labels_are_rejected_and_summary_is_separate() -> None:
    telemetry = collector()
    assert telemetry.scrape('minecraft', exposition((1, 2, 3, 4), labels='player="alice"')).reason == 'invalid_scrape'
    summary = '\n'.join(
        (
            'mc_server_tick_seconds{quantile="0.5"} 0.01',
            'mc_server_tick_seconds{quantile="0.95"} 0.02',
            'mc_server_tick_seconds{quantile="0.99"} 0.03',
        )
    )
    assert telemetry.verified_summary_window('minecraft', summary).reason == 'summary_window_unverified'
    verified = TickTelemetry(ExporterRegistry({'minecraft': ExporterSpec(
        'minecraft', 'http://127.0.0.1/metrics', PrometheusTickParser(), verified_summary_window=True
    )}))
    result = verified.verified_summary_window('minecraft', summary)
    assert result.state == 'available'
    assert (result.p50_ms, result.p95_ms, result.p99_ms) == (10.0, 20.0, 30.0)


@pytest.mark.parametrize('labels', (
    'le="0.01" garbage',
    'le="0.01",',
    'le="0.01" extra="x"',
    'le="0.01", broken',
    'le="0.01",bad="\\q"',
))
def test_label_parser_requires_complete_valid_syntax(labels: str) -> None:
    parser = PrometheusTickParser()
    with pytest.raises(ValueError, match='labels'):
        parser(f'mc_server_tick_seconds_bucket{{{labels}}} 1')


def test_histogram_family_cardinality_is_bounded() -> None:
    parser = PrometheusTickParser()
    lines = [
        f'mc_server_tick_seconds_bucket{{le="+Inf",world="w{index}"}} 1'
        for index in range(MAX_HISTOGRAMS_PER_PROFILE + 1)
    ]
    with pytest.raises(ValueError, match='family limit'):
        parser('\n'.join(lines))


def test_cumulative_histogram_validation_is_fail_closed() -> None:
    parser = PrometheusTickParser()
    with pytest.raises(ValueError, match='cumulative'):
        parser(exposition((2, 1, 3, 4)))
    with pytest.raises(ValueError, match='count mismatch'):
        parser(exposition((1, 2, 3, 4)).replace('seconds_count 4', 'seconds_count 5'))


def test_inf_delta_must_equal_count_delta() -> None:
    snapshots = iter((
        TickSnapshot({'': ((0.1, 3.0), (float('inf'), 4.0))}, {'': 0.04}, {'': 4.0}, {}),
        TickSnapshot({'': ((0.1, 4.0), (float('inf'), 7.0))}, {'': 0.06}, {'': 6.0}, {}),
    ))
    spec = ExporterSpec('minecraft', 'http://127.0.0.1/metrics', lambda _text: next(snapshots))
    telemetry = TickTelemetry(ExporterRegistry({'minecraft': spec}))
    telemetry.scrape('minecraft', '')
    result = telemetry.scrape('minecraft', '')
    assert result.reason == 'histogram_count_mismatch'


def test_summary_verification_cannot_be_enabled_per_call() -> None:
    telemetry = collector()
    with pytest.raises(TypeError):
        telemetry.verified_summary_window('minecraft', '', verified=True)  # type: ignore[call-arg]
    spec = ExporterSpec('minecraft', 'http://127.0.0.1/metrics', PrometheusTickParser(), True)
    with pytest.raises(AttributeError):
        spec.verified_summary_window = False  # type: ignore[misc]


@pytest.mark.parametrize('url', (
    'https://127.0.0.1/metrics',
    'http://example.com/metrics',
    'http://127.0.0.1:9100/other',
    'http://user:pass@127.0.0.1/metrics',
    'http://127.0.0.1/metrics?target=x',
))
def test_exporter_url_is_loopback_and_config_safe(url: str) -> None:
    with pytest.raises(ValueError, match='URL'):
        ExporterSpec('minecraft', url, PrometheusTickParser())


def test_unrelated_nan_metric_does_not_poison_target_family() -> None:
    parser = PrometheusTickParser()
    snapshot = parser('unrelated_metric NaN\n' + exposition((1, 2, 3, 4)))
    assert snapshot.counts == {'': 4.0}
