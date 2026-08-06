"""Fixed local lazymc supervisor/helper.

The helper is lazymc's long-lived ``server.command`` child, but it is only a
capability client, not a Java launcher or process owner. It requests a typed
wake from Horizon, observes the controller-owned health gate, then polls typed
status until Horizon reports its player-aware idle stop. It never accepts a
URL, profile, unit, path, command, or token from a caller and never signals a
Java process or issues a stop/restart action.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .capability import CAPABILITY_START_PROFILE


CAPABILITY_WAKE_URL = "http://127.0.0.1:8444/api/v1/capability/wake"
CAPABILITY_STATUS_URL = "http://127.0.0.1:8444/api/v1/capability/status"
CAPABILITY_AUDIENCE = "lazymc"
WAKE_TOKEN_CREDENTIAL = Path("/run/credentials/lazymc-minecraft.service/wake-token")
REQUEST_TIMEOUT_SECONDS = 5.0
STARTUP_GRACE_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 2.0
RUNNING_POLL_INTERVAL_SECONDS = 5.0
MAX_CONSECUTIVE_STATUS_FAILURES = 3


class LazyWakeError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def load_waker_token(path: Path = WAKE_TOKEN_CREDENTIAL) -> str:
    if Path(path) != WAKE_TOKEN_CREDENTIAL:
        raise LazyWakeError("wake credential path is not approved", retryable=False)
    try:
        token = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LazyWakeError("wake credential is unavailable", retryable=False) from exc
    token = token.strip()
    if not 40 <= len(token) <= 128 or "\n" in token or "\r" in token or "\x00" in token:
        raise LazyWakeError("wake credential is invalid", retryable=False)
    return token


class LazyWakeClient:
    """HTTP capability client with fixed endpoints and bounded responses."""

    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if not isinstance(token, str) or not 40 <= len(token) <= 128 or any(c in token for c in "\r\n\x00"):
            raise LazyWakeError("wake credential is invalid", retryable=False)
        self.token = token
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep

    def _post(self, url: str, request_id: str) -> dict[str, Any]:
        if url not in {CAPABILITY_WAKE_URL, CAPABILITY_STATUS_URL}:
            raise LazyWakeError("capability endpoint is not approved", retryable=False)
        body = json.dumps({"request_id": request_id, "action": {"kind": "wake" if url == CAPABILITY_WAKE_URL else "status"}}, separators=(",", ":")).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Horizon-Capability-Audience": CAPABILITY_AUDIENCE,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read(64 * 1024 + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise LazyWakeError("capability service unavailable") from exc
        if len(raw) > 64 * 1024:
            raise LazyWakeError("capability response exceeded its bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LazyWakeError("capability response was invalid") from exc
        if not isinstance(value, dict):
            raise LazyWakeError("capability response was invalid")
        return value

    @staticmethod
    def _error(value: dict[str, Any]) -> str | None:
        error = value.get("error")
        if not isinstance(error, dict):
            return None
        message = error.get("message")
        return message if isinstance(message, str) and len(message) <= 256 else "capability request failed"

    @staticmethod
    def _healthy(value: dict[str, Any]) -> bool:
        profiles = value.get("profiles")
        if not isinstance(profiles, list) or len(profiles) != 1 or not isinstance(profiles[0], dict):
            return False
        profile = profiles[0]
        return (
            profile.get("profile_id") == CAPABILITY_START_PROFILE.value
            and profile.get("state") == "running"
            and profile.get("health") == "healthy"
            and profile.get("required_ports_ready") is True
        )

    @staticmethod
    def _failed(value: dict[str, Any]) -> bool:
        profiles = value.get("profiles")
        return bool(
            isinstance(profiles, list)
            and len(profiles) == 1
            and isinstance(profiles[0], dict)
            and profiles[0].get("state") in {"failed", "blocked"}
        )

    def wake_and_wait(self, *, grace_seconds: float = STARTUP_GRACE_SECONDS) -> None:
        if not isinstance(grace_seconds, (int, float)) or not 1 <= grace_seconds <= STARTUP_GRACE_SECONDS:
            raise LazyWakeError("startup grace is outside its bound", retryable=False)
        wake = self._post(CAPABILITY_WAKE_URL, str(uuid4()))
        if self._error(wake) is not None:
            raise LazyWakeError("controller rejected wake")
        deadline = self.clock() + float(grace_seconds)
        while self.clock() < deadline:
            try:
                status = self._post(CAPABILITY_STATUS_URL, str(uuid4()))
            except LazyWakeError as exc:
                if not exc.retryable:
                    raise
                self.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - self.clock())))
                continue
            if self._error(status) is not None:
                raise LazyWakeError("controller status failed")
            if self._healthy(status):
                return
            if self._failed(status):
                raise LazyWakeError("controller health gate failed")
            self.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - self.clock())))
        raise LazyWakeError("controller health gate timed out")

    def wait_until_stopped(self) -> None:
        failures = 0
        while True:
            try:
                status = self._post(CAPABILITY_STATUS_URL, str(uuid4()))
                if self._error(status) is not None:
                    raise LazyWakeError("controller status failed")
                failures = 0
            except LazyWakeError:
                failures += 1
                if failures >= MAX_CONSECUTIVE_STATUS_FAILURES:
                    raise
                self.sleep(POLL_INTERVAL_SECONDS)
                continue
            profiles = status.get("profiles")
            if isinstance(profiles, list) and len(profiles) == 1 and isinstance(profiles[0], dict):
                state = profiles[0].get("state")
                if state == "stopped":
                    return
                if state in {"failed", "blocked"}:
                    raise LazyWakeError("controller reported a failed game", retryable=False)
            self.sleep(RUNNING_POLL_INTERVAL_SECONDS)


def run_wake_hook(*, token_path: Path = WAKE_TOKEN_CREDENTIAL) -> int:
    try:
        client = LazyWakeClient(load_waker_token(token_path))
        client.wake_and_wait()
        client.wait_until_stopped()
        return 0
    except LazyWakeError as exc:
        return 75 if exc.retryable else 78


__all__ = [
    "CAPABILITY_AUDIENCE",
    "CAPABILITY_STATUS_URL",
    "CAPABILITY_WAKE_URL",
    "LazyWakeClient",
    "LazyWakeError",
    "POLL_INTERVAL_SECONDS",
    "RUNNING_POLL_INTERVAL_SECONDS",
    "STARTUP_GRACE_SECONDS",
    "WAKE_TOKEN_CREDENTIAL",
    "load_waker_token",
    "run_wake_hook",
]
