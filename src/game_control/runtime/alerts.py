"""Bounded typed alert delivery runtime.

The runtime owns only delivery tasks it creates.  Notification service and
HTTP-client lifecycle remain composition-root concerns until integration.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from typing import Any

from ..alert_policy import AlertEmission, AlertSignal, PerformanceAlertEvaluator
from ..models import NotificationEvent
from ..notifications import NotificationService
from .protocols import AlertObservation, AlertSink

_ALLOWED_SIGNALS = frozenset({
    AlertSignal.SUSTAINED_MSPT,
    AlertSignal.MEMORY_GROWTH,
    AlertSignal.WAKE_SLO,
    AlertSignal.BENCHMARK_REGRESSION,
})
_STATES = frozenset({"starting", "running", "stopped", "stopping", "failed"})


def _profile_key(value: Any) -> str:
    return str(getattr(value, "value", value))


async def _drain(tasks: tuple[asyncio.Task[Any], ...]) -> bool:
    """Drain accepted tasks despite repeated cancellation."""

    if not tasks:
        return False
    pending = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while True:
        try:
            await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            cancelled = True
            continue
    return cancelled


class AlertRuntime(AlertSink):
    """Translate strict observations to the existing fixed alert policy."""

    def __init__(
        self,
        profiles: Mapping[str, Any],
        notifications: NotificationService,
        *,
        evaluator: PerformanceAlertEvaluator | None = None,
        max_pending: int = 8,
    ) -> None:
        if not isinstance(profiles, Mapping):
            raise TypeError("profiles must be a mapping")
        self.profiles = profiles
        self.notifications = notifications
        self.evaluator = evaluator or PerformanceAlertEvaluator()
        self.max_pending = max(1, min(32, int(max_pending)))
        self._pending: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._closing = False
        self.dropped = 0
        self.failures = 0
        self.cancelled = 0
        self.cancellations = 0

    def _profile(self, profile_id: str) -> Any:
        try:
            return self.profiles[profile_id]
        except (KeyError, TypeError):
            for key, profile in self.profiles.items():
                if _profile_key(key) == profile_id or _profile_key(getattr(profile, "id", "")) == profile_id:
                    return profile
        raise ValueError("unknown alert profile")

    @staticmethod
    def _validate(observation: AlertObservation) -> None:
        if observation.profile_state not in _STATES:
            raise ValueError("invalid alert profile state")
        if (
            isinstance(observation.now, bool)
            or not isinstance(observation.now, (int, float))
            or not math.isfinite(float(observation.now))
            or float(observation.now) < 0
        ):
            raise ValueError("invalid alert timestamp")
        for value, name in (
            (observation.mspt_p95, "mspt_p95"),
            (observation.rss_bytes, "rss_bytes"),
            (observation.wake_duration_ms, "wake_duration_ms"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"invalid {name}")
        if observation.benchmark_regression is not None and not isinstance(observation.benchmark_regression, bool):
            raise ValueError("invalid benchmark regression state")

    def observe(self, observation: AlertObservation) -> None:
        if not isinstance(observation, AlertObservation):
            raise TypeError("alert observation must be AlertObservation")
        if self._closed or self._closing:
            self.dropped += 1
            return
        self._validate(observation)
        profile = self._profile(observation.profile_id)
        emissions = self.evaluator.observe(
            observation.profile_id,
            profile_state=observation.profile_state,
            now=observation.now,
            mspt_p95=observation.mspt_p95,
            rss_bytes=observation.rss_bytes,
            wake_duration_ms=observation.wake_duration_ms,
            benchmark_regression=observation.benchmark_regression,
        )
        for emission in emissions:
            if not isinstance(emission, AlertEmission) or emission.signal not in _ALLOWED_SIGNALS:
                continue
            if len(self._pending) >= self.max_pending:
                self.dropped += 1
                continue
            task = asyncio.create_task(
                self.notifications.send_async(
                    profile,
                    NotificationEvent(emission.signal.value),
                    emission.generation,
                    emission.message,
                ),
                name=f"horizon-alert-{emission.signal.value}",
            )
            self._pending.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            self.cancelled += 1
            self.cancellations += 1
        except BaseException:
            self.failures += 1

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        accepted = tuple(self._pending)
        cancelled = await _drain(accepted)
        # Done callbacks are scheduled independently of gather completion;
        # the close contract nevertheless reports no accepted work pending.
        self._pending.difference_update(accepted)
        self._closed = True
        self._closing = False
        if cancelled:
            raise asyncio.CancelledError

    def health(self) -> Mapping[str, Any]:
        return {
            "closed": self._closed,
            "pending": len(self._pending),
            "dropped": self.dropped,
            "failures": self.failures,
            "cancelled": self.cancelled,
            "cancellations": self.cancellations,
        }


__all__ = ["AlertRuntime"]
