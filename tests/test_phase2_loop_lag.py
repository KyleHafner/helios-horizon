import pytest


def test_loop_lag_scheduling_does_not_catch_up_after_long_stall():
    interval = 0.25
    deadline = 0.25
    now = 1.25
    lag = max(0.0, (now - deadline) * 1000.0)
    next_deadline = now + interval
    assert lag == pytest.approx(1000.0)
    assert next_deadline == pytest.approx(1.5)
    assert next_deadline > now
