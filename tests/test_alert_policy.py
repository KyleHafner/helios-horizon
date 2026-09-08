from types import SimpleNamespace

import pytest

from game_control.alert_policy import (
    ALERT_POLICY,
    AlertOwner,
    AlertSignal,
    PerformanceAlertEvaluator,
)
from game_control.models import NotificationEvent
from game_control.service_wiring import _PerformanceAlerts


def test_every_fixed_signal_has_one_exclusive_owner():
    assert set(ALERT_POLICY) == set(AlertSignal)
    assert len(ALERT_POLICY) == len(AlertSignal)
    assert ALERT_POLICY[AlertSignal.DISK_PRESSURE].owner is AlertOwner.PROMETHEUS_ALERTMANAGER
    assert all(
        rule.owner is AlertOwner.SLOTD
        for signal, rule in ALERT_POLICY.items()
        if signal is not AlertSignal.DISK_PRESSURE
    )


def test_sustained_mspt_window_emits_once_recovers_and_can_realert():
    evaluator = PerformanceAlertEvaluator(generation_clock=iter(range(10, 20)).__next__)
    assert evaluator.observe("minecraft", profile_state="running", now=0, mspt_p95=55) == ()
    assert evaluator.observe("minecraft", profile_state="running", now=10, mspt_p95=56) == ()
    assert evaluator.observe("minecraft", profile_state="running", now=20, mspt_p95=57) == ()
    first = evaluator.observe("minecraft", profile_state="running", now=30, mspt_p95=58)
    assert [item.signal for item in first] == [AlertSignal.SUSTAINED_MSPT]
    assert evaluator.observe("minecraft", profile_state="running", now=35, mspt_p95=60) == ()
    assert evaluator.observe("minecraft", profile_state="running", now=40, mspt_p95=20) == ()
    for stamp in (50, 60, 70):
        assert evaluator.observe("minecraft", profile_state="running", now=stamp, mspt_p95=60) == ()
    again = evaluator.observe("minecraft", profile_state="running", now=80, mspt_p95=60)
    assert [item.signal for item in again] == [AlertSignal.SUSTAINED_MSPT]
    assert again[0].generation > first[0].generation


def test_sustained_windows_tolerate_fractional_polling_jitter():
    evaluator = PerformanceAlertEvaluator()
    emissions = []
    for index in range(121):
        emissions.extend(evaluator.observe(
            "minecraft", profile_state="running", now=index * 10.001,
            mspt_p95=100, rss_bytes=1_000_000_000 + index * 16 * 1024 * 1024,
        ))
    assert [item.signal for item in emissions] == [AlertSignal.SUSTAINED_MSPT]
    assert AlertSignal.SUSTAINED_MSPT in evaluator.active_signals("minecraft")


def test_memory_growth_window_tolerates_fractional_polling_jitter_without_mspt():
    evaluator = PerformanceAlertEvaluator()
    emissions = []
    for index in range(121):
        emissions.extend(evaluator.observe(
            "minecraft", profile_state="running", now=index * 10.001,
            rss_bytes=1_000_000_000 + index * 16 * 1024 * 1024,
        ))
    assert [item.signal for item in emissions] == [AlertSignal.MEMORY_GROWTH]


def test_sustained_windows_need_enough_observations_after_a_gap_and_reset():
    evaluator = PerformanceAlertEvaluator()
    evaluator.observe("minecraft", profile_state="running", now=0, mspt_p95=100)
    assert evaluator.observe("minecraft", profile_state="running", now=31, mspt_p95=100) == ()
    for stamp in (41, 51, 61):
        evaluator.observe("minecraft", profile_state="running", now=stamp, mspt_p95=100)
    assert evaluator.active_signals("minecraft") == (AlertSignal.SUSTAINED_MSPT,)
    evaluator.observe("minecraft", profile_state="stopped", now=62, mspt_p95=100)
    assert evaluator.active_signals("minecraft") == ()


def test_memory_growth_is_root_cause_exclusive_with_mspt():
    evaluator = PerformanceAlertEvaluator(generation_clock=lambda: 50)
    evaluator.observe("minecraft", profile_state="running", now=0, mspt_p95=60, rss_bytes=1_000_000_000)
    evaluator.observe("minecraft", profile_state="running", now=570, mspt_p95=60)
    evaluator.observe("minecraft", profile_state="running", now=580, mspt_p95=60)
    evaluator.observe("minecraft", profile_state="running", now=590, mspt_p95=60)
    # Both rules become true at this evaluation. Memory is the higher-priority
    # root cause, so only one notification leaves the runtime-pressure group.
    emitted = evaluator.observe(
        "minecraft", profile_state="running", now=600, mspt_p95=60,
        rss_bytes=1_000_000_000 + 512 * 1024 * 1024,
    )
    assert [item.signal for item in emitted] == [AlertSignal.MEMORY_GROWTH]


def test_memory_incident_does_not_recover_when_baseline_only_ages_out():
    evaluator = PerformanceAlertEvaluator()
    baseline = 1_000_000_000
    evaluator.observe("minecraft", profile_state="running", now=0, rss_bytes=baseline)
    evaluator.observe(
        "minecraft", profile_state="running", now=600,
        rss_bytes=baseline + 512 * 1024 * 1024,
    )
    evaluator.observe(
        "minecraft", profile_state="running", now=601,
        rss_bytes=baseline + 512 * 1024 * 1024,
    )
    assert AlertSignal.MEMORY_GROWTH in evaluator.active_signals("minecraft")
    evaluator.observe(
        "minecraft", profile_state="running", now=602,
        rss_bytes=baseline + 255 * 1024 * 1024,
    )
    assert AlertSignal.MEMORY_GROWTH not in evaluator.active_signals("minecraft")


def test_inactive_suppresses_runtime_and_clears_sustained_window():
    evaluator = PerformanceAlertEvaluator()
    for stamp in (0, 10, 20):
        evaluator.observe("minecraft", profile_state="running", now=stamp, mspt_p95=80)
    assert evaluator.observe("minecraft", profile_state="stopped", now=30, mspt_p95=80) == ()
    assert evaluator.observe("minecraft", profile_state="running", now=40, mspt_p95=80) == ()


def test_wake_and_benchmark_rules_are_bounded_and_recover():
    evaluator = PerformanceAlertEvaluator(generation_clock=lambda: 100)
    wake = evaluator.observe("minecraft", profile_state="running", now=1, wake_duration_ms=180_001)
    assert [item.signal for item in wake] == [AlertSignal.WAKE_SLO]
    assert evaluator.observe("minecraft", profile_state="running", now=2, wake_duration_ms=1_000) == ()
    regression = evaluator.observe("minecraft", profile_state="stopped", now=3, benchmark_regression=True)
    assert [item.signal for item in regression] == [AlertSignal.BENCHMARK_REGRESSION]
    assert evaluator.observe("minecraft", profile_state="stopped", now=4, benchmark_regression=False) == ()


def test_inactive_transition_clears_event_scoped_wake_breach():
    evaluator = PerformanceAlertEvaluator()
    assert evaluator.observe(
        "minecraft", profile_state="starting", now=1, wake_duration_ms=180_001,
    )
    assert evaluator.observe("minecraft", profile_state="stopped", now=2) == ()
    assert AlertSignal.WAKE_SLO not in evaluator.active_signals("minecraft")
    assert evaluator.observe("minecraft", profile_state="starting", now=3) == ()


def test_invalid_numeric_input_never_creates_a_breach():
    evaluator = PerformanceAlertEvaluator()
    assert evaluator.observe("minecraft", profile_state="running", now=0, mspt_p95=float("nan")) == ()
    with pytest.raises(ValueError, match="timestamp"):
        evaluator.observe("minecraft", profile_state="running", now=float("inf"))


@pytest.mark.asyncio
async def test_slotd_alert_bridge_uses_existing_typed_notification_dedup_path():
    calls = []

    class Notifications:
        async def send_async(self, profile, event, generation, message):
            calls.append((profile.id, event, generation, message))
            return True

    profile = SimpleNamespace(id="minecraft")
    alerts = _PerformanceAlerts({"minecraft": profile}, Notifications())
    alerts.observe(
        "minecraft", profile_state="running", now=1,
        wake_duration_ms=180_001,
    )
    alerts.observe(
        "minecraft", profile_state="running", now=2,
        wake_duration_ms=180_002,
    )
    await alerts.close()

    assert len(calls) == 1
    assert calls[0][1] is NotificationEvent.WAKE_SLO
    assert "180 second" in calls[0][3]
