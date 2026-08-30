"""Explicit lifecycle owner for the assembled Horizon services.

The container owns typed services, never their underlying clients.  Resource
refs make borrowed versus owned state visible at the composition boundary and
keep modern telemetry ownership in ``TelemetryRuntime``.
"""

from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

from .runtime import ResourceRef

if TYPE_CHECKING:
    from .adapters.crafty import CraftyAdapter
    from .controller import Controller
    from .history_queries import HistoryQueryService
    from .notifications import NotificationService
    from .runtime.alerts import AlertRuntime
    from .runtime.telemetry import TelemetryRuntime
    from .tps import TpsSampler
    from .updates import UpdateService


class ServiceContainer:
    """Close owned resources in the single application-level order."""

    def __init__(
        self,
        *,
        controller: "Controller",
        state_database: ResourceRef[Any],
        telemetry_runtime: ResourceRef["TelemetryRuntime"],
        alert_runtime: ResourceRef["AlertRuntime"],
        history_queries: ResourceRef["HistoryQueryService"],
        crafty_adapters: tuple[ResourceRef["CraftyAdapter"], ...] = (),
        update_services: tuple[ResourceRef["UpdateService"], ...] = (),
        notification_service: ResourceRef["NotificationService"] | None = None,
        legacy_tps_sampler: ResourceRef["TpsSampler"] | None = None,
    ) -> None:
        self.controller = controller
        self.state_database = state_database
        self.telemetry_runtime = telemetry_runtime
        self.alert_runtime = alert_runtime
        self.history_queries = history_queries
        self.crafty_adapters = tuple(crafty_adapters)
        self.update_services = tuple(update_services)
        self.notification_service = notification_service
        self.legacy_tps_sampler = legacy_tps_sampler
        self._closed = False
        self._first_close_error: BaseException | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._controller_closed = False
        self._telemetry_closed = False
        self._alerts_closed = False
        self._legacy_sampler_closed = False
        self._crafty_closed: set[int] = set()
        self._updates_closed: set[int] = set()
        self._notification_closed = False
        self._history_closed = False
        self._state_closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def _close_controller(self) -> None:
        if self._controller_closed:
            return
        await self.controller.aclose()
        self._controller_closed = True

    async def _close_telemetry(self) -> None:
        if self._telemetry_closed:
            return
        if not self.telemetry_runtime.owns_value:
            self._telemetry_closed = True
            return
        await self.telemetry_runtime.value.close()
        self._telemetry_closed = True

    async def _close_alerts(self) -> None:
        if self._alerts_closed:
            return
        if not self.alert_runtime.owns_value:
            self._alerts_closed = True
            return
        await self.alert_runtime.value.close()
        self._alerts_closed = True

    async def _close_legacy_sampler(self) -> None:
        if self._legacy_sampler_closed:
            return
        ref = self.legacy_tps_sampler
        if ref is None or not ref.owns_value:
            self._legacy_sampler_closed = True
            return
        close = ref.value.aclose
        await close()
        self._legacy_sampler_closed = True

    async def _close_crafty_adapters(self) -> None:
        # A shared adapter can be present in more than one profile seam.  The
        # object identity, rather than tuple position, is the close ledger.
        first_error: BaseException | None = None
        cancelled = False
        attempted: set[int] = set()
        for ref in self.crafty_adapters:
            identity = id(ref.value)
            if identity in self._crafty_closed or identity in attempted:
                continue
            if not ref.owns_value:
                continue
            attempted.add(identity)
            try:
                await ref.value.aclose()
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                self._crafty_closed.add(identity)
        if first_error is not None:
            raise first_error
        if cancelled:
            raise asyncio.CancelledError

    async def _close_update_services(self) -> None:
        first_error: BaseException | None = None
        cancelled = False
        attempted: set[int] = set()
        for ref in self.update_services:
            identity = id(ref.value)
            if identity in self._updates_closed or identity in attempted:
                continue
            if not ref.owns_value:
                continue
            attempted.add(identity)
            try:
                await ref.value.aclose()
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                self._updates_closed.add(identity)
        if first_error is not None:
            raise first_error
        if cancelled:
            raise asyncio.CancelledError

    async def _close_notification(self) -> None:
        if self._notification_closed:
            return
        ref = self.notification_service
        if ref is None or not ref.owns_value:
            self._notification_closed = True
            return
        await asyncio.to_thread(ref.value.close)
        self._notification_closed = True

    async def _close_history(self) -> None:
        if self._history_closed:
            return
        if not self.history_queries.owns_value:
            self._history_closed = True
            return
        await self.history_queries.value.aclose()
        self._history_closed = True

    def _close_state_database(self) -> None:
        if self._state_closed:
            return
        if not self.state_database.owns_value:
            self._state_closed = True
            return
        # StateDatabase's sqlite connection retains owner-thread affinity.
        self.state_database.value.close()
        self._state_closed = True

    async def _close_impl(self) -> None:
        first_error: BaseException | None = None
        cancelled = False

        async def run_async(stage) -> None:
            nonlocal first_error, cancelled
            try:
                await stage()
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as exc:
                if first_error is None:
                    first_error = exc

        def run_sync(stage) -> None:
            nonlocal first_error
            try:
                stage()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc

        await run_async(self._close_controller)
        await run_async(self._close_telemetry)
        await run_async(self._close_alerts)
        await run_async(self._close_legacy_sampler)
        await run_async(self._close_crafty_adapters)
        await run_async(self._close_update_services)
        await run_async(self._close_notification)
        await run_async(self._close_history)
        run_sync(self._close_state_database)

        if first_error is not None:
            if self._first_close_error is None:
                self._first_close_error = first_error
            raise self._first_close_error
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _consume_close_task(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except BaseException:
            return

    async def aclose(self) -> None:
        """Drain all close stages, preserving cancellation and first errors."""

        self._closed = True
        task = self._close_task
        failed = False
        if task is not None and task.done() and not task.cancelled():
            try:
                failed = task.exception() is not None
            except BaseException:
                failed = True
        if task is None or (task.done() and (task.cancelled() or failed)):
            task = asyncio.create_task(self._close_impl(), name="horizon-service-container-close")
            task.add_done_callback(self._consume_close_task)
            self._close_task = task
        elif task.done():
            # A completed successful close is fully idempotent.
            return

        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    break
        error: BaseException | None = None
        try:
            task.result()
        except BaseException as exc:
            error = exc
        if cancelled or isinstance(error, asyncio.CancelledError):
            raise asyncio.CancelledError
        if error is not None:
            if self._first_close_error is not None:
                raise self._first_close_error
            raise error
        if self._first_close_error is not None:
            raise self._first_close_error

    def close(self) -> None:
        """Synchronously close when no event loop is running."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("ServiceContainer.close() cannot run inside an event loop; use await aclose()")


__all__ = ["ServiceContainer"]
