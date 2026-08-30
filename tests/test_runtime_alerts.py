from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from game_control.alert_policy import AlertEmission, AlertSignal
from game_control.models import NotificationEvent
from game_control.runtime import AlertObservation, AlertRuntime


def _profile() -> SimpleNamespace:
    return SimpleNamespace(id="minecraft")


class _Notifications:
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = block
        self.fail = fail

    async def send_async(self, profile, event, generation, message):
        self.calls.append((profile, event, generation, message))
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("delivery failed")
        return True


class _EveryObservation:
    def observe(self, profile_id, **_values):
        return (AlertEmission(AlertSignal.WAKE_SLO, 9, "bounded alert"),)


class _DiskEmission:
    def observe(self, profile_id, **_values):
        return (AlertEmission(AlertSignal.DISK_PRESSURE, 9, "must not deliver"),)


def _observation(**changes):
    values = {
        "profile_id": "minecraft",
        "profile_state": "running",
        "now": 1.0,
    }
    values.update(changes)
    return AlertObservation(**values)


def test_runtime_accepts_only_typed_known_profile_and_allowlisted_emissions():
    notifications = _Notifications()
    runtime = AlertRuntime({"minecraft": _profile()}, notifications, evaluator=_DiskEmission())
    with pytest.raises(TypeError):
        runtime.observe({"profile_id": "minecraft"})
    with pytest.raises(ValueError, match="unknown"):
        runtime.observe(_observation(profile_id="unknown"))
    runtime.observe(_observation())
    assert notifications.calls == []


@pytest.mark.asyncio
async def test_runtime_translates_fixed_emissions_and_preserves_order():
    notifications = _Notifications()
    runtime = AlertRuntime({"minecraft": _profile()}, notifications, evaluator=_EveryObservation())
    runtime.observe(_observation())
    await runtime.close()
    assert notifications.calls == [(_profile(), NotificationEvent.WAKE_SLO, 9, "bounded alert")]


@pytest.mark.asyncio
async def test_runtime_bounds_pending_and_accounts_failure():
    notifications = _Notifications(block=True, fail=True)
    runtime = AlertRuntime({"minecraft": _profile()}, notifications, evaluator=_EveryObservation(), max_pending=1)
    runtime.observe(_observation())
    runtime.observe(_observation(now=2))
    assert runtime.dropped == 1
    notifications.release.set()
    await runtime.close()
    assert runtime.failures == 1
    assert runtime.health()["pending"] == 0


@pytest.mark.asyncio
async def test_runtime_close_drains_and_preserves_repeated_cancellation():
    notifications = _Notifications(block=True)
    runtime = AlertRuntime({"minecraft": _profile()}, notifications, evaluator=_EveryObservation())
    runtime.observe(_observation())
    close_task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    close_task.cancel()
    close_task.cancel()
    await asyncio.sleep(0)
    notifications.release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert runtime.health()["closed"] is True
    runtime.observe(_observation(now=2))
    assert runtime.dropped == 1
    assert len(notifications.calls) == 1


@pytest.mark.asyncio
async def test_runtime_delivery_cancellation_is_accounted_without_callback_warning():
    notifications = _Notifications(block=True)
    runtime = AlertRuntime({"minecraft": _profile()}, notifications, evaluator=_EveryObservation())
    runtime.observe(_observation())
    await notifications.started.wait()
    task = next(iter(runtime._pending))
    task.cancel()
    notifications.release.set()
    await runtime.close()
    assert runtime.cancelled == 1
    assert runtime.health()["pending"] == 0
