import asyncio
import inspect

import pytest

from game_control.runtime import ResourceRef
from game_control.service_container import ServiceContainer


class AsyncClose:
    def __init__(self, name, events, *, gate=None, error=None):
        self.name = name
        self.events = events
        self.gate = gate
        self.error = error
        self.calls = 0

    async def close(self):
        self.calls += 1
        self.events.append(self.name)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            error, self.error = self.error, None
            raise error

    async def aclose(self):
        await self.close()


class SyncClose:
    def __init__(self, name, events):
        self.name = name
        self.events = events
        self.calls = 0

    def close(self):
        self.calls += 1
        self.events.append(self.name)


def _container(events, *, owned=True, controller=None, telemetry=None):
    marker = lambda name: AsyncClose(name, events)
    controller = controller or marker("controller")
    telemetry = telemetry or marker("telemetry")
    return ServiceContainer(
        controller=controller,
        state_database=ResourceRef.owned(SyncClose("state", events)) if owned else ResourceRef.borrowed(SyncClose("state", events)),
        telemetry_runtime=ResourceRef.owned(telemetry) if owned else ResourceRef.borrowed(telemetry),
        alert_runtime=ResourceRef.owned(marker("alerts")) if owned else ResourceRef.borrowed(marker("alerts")),
        history_queries=ResourceRef.owned(marker("history")) if owned else ResourceRef.borrowed(marker("history")),
        crafty_adapters=(ResourceRef.owned(marker("crafty")),) if owned else (ResourceRef.borrowed(marker("crafty")),),
        update_services=(ResourceRef.owned(marker("updates")),) if owned else (ResourceRef.borrowed(marker("updates")),),
        notification_service=ResourceRef.owned(SyncClose("notifications", events)) if owned else ResourceRef.borrowed(SyncClose("notifications", events)),
        legacy_tps_sampler=ResourceRef.owned(marker("legacy")) if owned else ResourceRef.borrowed(marker("legacy")),
    )


@pytest.mark.asyncio
async def test_close_order_and_borrowed_resources():
    events = []
    container = _container(events)
    await container.aclose()
    await container.aclose()
    assert events == [
        "controller", "telemetry", "alerts", "legacy", "crafty",
        "updates", "notifications", "history", "state",
    ]

    borrowed_events = []
    borrowed = _container(borrowed_events, owned=False)
    await borrowed.aclose()
    assert borrowed_events == ["controller"]


@pytest.mark.asyncio
async def test_close_continues_after_first_error_and_retries_failed_stage():
    events = []
    telemetry = AsyncClose("telemetry", events, error=RuntimeError("first"))
    container = _container(events, telemetry=telemetry)
    with pytest.raises(RuntimeError, match="first"):
        await container.aclose()
    assert events == [
        "controller", "telemetry", "alerts", "legacy", "crafty",
        "updates", "notifications", "history", "state",
    ]
    with pytest.raises(RuntimeError, match="first"):
        await container.aclose()
    assert events.count("telemetry") == 2
    assert events.count("state") == 1


@pytest.mark.asyncio
async def test_each_typed_owner_is_attempted_after_a_sibling_failure():
    events = []
    first_adapter = AsyncClose("crafty-1", events, error=RuntimeError("adapter"))
    second_adapter = AsyncClose("crafty-2", events)
    first_update = AsyncClose("update-1", events, error=RuntimeError("update"))
    second_update = AsyncClose("update-2", events)
    container = _container(events)
    container.crafty_adapters = (ResourceRef.owned(first_adapter), ResourceRef.owned(second_adapter))
    container.update_services = (ResourceRef.owned(first_update), ResourceRef.owned(second_update))
    with pytest.raises(RuntimeError, match="adapter"):
        await container.aclose()
    assert events.index("crafty-2") > events.index("crafty-1")
    assert events.index("update-2") > events.index("update-1")
    with pytest.raises(RuntimeError, match="adapter"):
        await container.aclose()
    assert first_adapter.calls == 2
    assert second_adapter.calls == 1
    assert first_update.calls == 2
    assert second_update.calls == 1


@pytest.mark.asyncio
async def test_cancellation_drains_all_stages_and_preserves_lease_cleanup():
    events = []
    gate = asyncio.Event()
    controller = AsyncClose("controller", events, gate=gate)
    container = _container(events, controller=controller)
    closing = asyncio.create_task(container.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    closing.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert events == [
        "controller", "telemetry", "alerts", "legacy", "crafty",
        "updates", "notifications", "history", "state",
    ]
    assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]


def test_sync_close_rejects_running_event_loop():
    async def check():
        with pytest.raises(RuntimeError, match="use await aclose"):
            _container([]).close()

    asyncio.run(check())


def test_api_has_typed_resource_refs_only():
    signature = inspect.signature(ServiceContainer)
    assert "supervisor_tasks" not in signature.parameters
    assert "telemetry_db" not in signature.parameters
    assert "rcon" not in signature.parameters
