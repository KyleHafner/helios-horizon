"""Secret-safe notification delivery from root-owned configuration.

Notification destinations are deliberately not part of the RPC contract.  A
profile selects events in its root-owned TOML file and operators may only
toggle those events in the state database.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx

from .errors import SafeError
from .models import NotificationEvent
from .protocol import NotificationConfig, NotificationTarget
from .redaction import Redactor, SecretRegistry

CHANNELS = ("discord", "telegram")
DEFAULT_SECRET_DIR = Path("/etc/game-control/secrets.d")
TIMEOUT_SECONDS = 10.0


def _profile_id(profile: Any) -> str:
    value = getattr(profile, "id", profile)
    return getattr(value, "value", str(value))


def _connection(database: Any) -> sqlite3.Connection | None:
    if isinstance(database, sqlite3.Connection):
        return database
    return getattr(database, "connection", None)


class NotificationService:
    """Deliver notifications using only installed, root-local secrets."""

    def __init__(
        self,
        profiles: Mapping[str, Any] | Any,
        *,
        secret_dir: str | os.PathLike[str] = DEFAULT_SECRET_DIR,
        database: Any | None = None,
        http_client: Any | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], float] | None = None,
        low_disk_cooldown_seconds: float = 300.0,
        telegram_chat_id: str = "game-control",
    ) -> None:
        if isinstance(profiles, Mapping):
            self.profiles = profiles
        elif hasattr(profiles, "id"):
            self.profiles = {_profile_id(profiles): profiles}
        else:
            self.profiles = {_profile_id(item): item for item in profiles}
        self.secret_dir = Path(secret_dir)
        self.database = database
        # ``http_client`` is an explicitly injected, borrowed seam.  Do not
        # use truthiness here: test doubles and adapters may deliberately be
        # falsey while still being valid clients.
        self._owns_http_client = http_client is None
        self.http_client = (
            httpx.Client(timeout=TIMEOUT_SECONDS) if http_client is None else http_client
        )
        self._http_client_closed = False
        self.timeout_seconds = TIMEOUT_SECONDS
        self.redactor = redactor or Redactor(SecretRegistry())
        self.clock = clock or time.monotonic
        self.low_disk_cooldown_seconds = float(low_disk_cooldown_seconds)
        self.telegram_chat_id = telegram_chat_id
        self._low_disk_sent: dict[str, float] = {}
        self.last_error: str | None = None

    def _get_profile(self, profile: Any) -> Any:
        key = _profile_id(profile)
        try:
            return self.profiles[key]
        except (KeyError, TypeError) as exc:
            raise SafeError("profile_not_found", "profile was not found") from exc

    def _secret(self, channel: str) -> str:
        if channel not in CHANNELS:
            raise SafeError("notification_failed", "notification channel is unavailable")
        path = self.secret_dir / channel
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            secret = path.read_text(encoding="utf-8").strip()
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o077:
                raise OSError
            if not secret:
                raise OSError
            return secret
        except (OSError, UnicodeError) as exc:
            raise SafeError("notification_unconfigured", "notification channel is not configured") from exc

    def _target(self, channel: str, secret: str) -> str:
        # Discord secrets are installed webhook URLs.  Telegram secrets are
        # bot tokens; the API host and chat destination are fixed here.
        if channel == "discord":
            if not secret.startswith(("https://", "http://")):
                raise SafeError("notification_unconfigured", "notification channel is not configured")
            return secret
        if any(char in secret for char in "/?#\r\n"):
            raise SafeError("notification_unconfigured", "notification channel is not configured")
        return f"https://api.telegram.org/bot{secret}/sendMessage"

    def _enabled(self, profile: Any, event: NotificationEvent) -> bool:
        events = getattr(profile, "notification_events", frozenset())
        if event not in events and event.value not in events:
            return False
        connection = _connection(self.database)
        if connection is None:
            return True
        row = connection.execute(
            "SELECT enabled FROM notification_rules WHERE profile_id=? AND event=?",
            (_profile_id(profile), event.value),
        ).fetchone()
        return True if row is None else bool(row[0])

    def _audit(self, actor: str, action: str, profile_id: str, result: str, error_code: str | None = None) -> None:
        connection = _connection(self.database)
        if connection is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "INSERT INTO audit(id,timestamp,actor,action,profile_id,result,error_code,detail) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, now, actor[:128], action, profile_id, result, error_code, "notification test"),
        )
        connection.commit()

    def _already_delivered(self, profile_id: str, event: str, generation: int, channel: str) -> bool:
        connection = _connection(self.database)
        if connection is None:
            return False
        key = f"{profile_id}:{event}:{generation}:{channel}"
        row = connection.execute("SELECT 1 FROM notification_deliveries WHERE id=?", (key,)).fetchone()
        return row is not None

    def _record_delivery(
        self, profile_id: str, event: str, generation: int, channel: str, error_code: str | None = None
    ) -> None:
        connection = _connection(self.database)
        if connection is None:
            return
        key = f"{profile_id}:{event}:{generation}:{channel}"
        now = datetime.now(timezone.utc).isoformat() if error_code is None else None
        connection.execute(
            "INSERT OR IGNORE INTO notification_deliveries"
            "(id,profile_id,event,state_generation,channel,delivered_at,error_code) VALUES(?,?,?,?,?,?,?)",
            (key, profile_id, event, generation, channel, now, error_code),
        )
        connection.commit()

    def _post(self, channel: str, secret: str, message: str) -> None:
        # Register before constructing a request so transport and error
        # handlers can never expose an installed value.
        self.redactor.registry.add(secret)
        # Redactor snapshots configured values at construction; rebuild its
        # matcher after registration while retaining the shared registry.
        self.redactor = Redactor(self.redactor.registry)
        url = self._target(channel, secret)
        payload = {"content": message} if channel == "discord" else {
            "chat_id": self.telegram_chat_id,
            "text": message,
        }
        try:
            response = self.http_client.post(url, json=payload, timeout=TIMEOUT_SECONDS)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
        except Exception as exc:
            self.last_error = self.redactor.redact(str(exc))
            raise SafeError("notification_failed", "notification delivery failed", retryable=True) from None

    def send(
        self,
        profile: Any,
        event: NotificationEvent | str,
        state_generation: int,
        message: str,
    ) -> bool:
        profile_obj = self._get_profile(profile)
        try:
            event_obj = event if isinstance(event, NotificationEvent) else NotificationEvent(event)
        except ValueError as exc:
            raise SafeError("notification_failed", "notification event is unavailable") from exc
        profile_key = _profile_id(profile_obj)
        if not self._enabled(profile_obj, event_obj):
            return False
        now = self.clock()
        if event_obj is NotificationEvent.LOW_DISK:
            previous = self._low_disk_sent.get(profile_key)
            if previous is not None and now - previous < self.low_disk_cooldown_seconds:
                return False
        delivered = False
        for channel in CHANNELS:
            try:
                secret = self._secret(channel)
            except SafeError:
                continue
            if self._already_delivered(profile_key, event_obj.value, int(state_generation), channel):
                continue
            self._post(channel, secret, message)
            self._record_delivery(profile_key, event_obj.value, int(state_generation), channel)
            delivered = True
        if delivered and event_obj is NotificationEvent.LOW_DISK:
            self._low_disk_sent[profile_key] = now
        return delivered

    async def send_async(
        self,
        profile: Any,
        event: NotificationEvent | str,
        state_generation: int,
        message: str,
    ) -> bool:
        """Keep SQLite access on the owner thread while offloading HTTP I/O."""
        profile_obj = self._get_profile(profile)
        try:
            event_obj = event if isinstance(event, NotificationEvent) else NotificationEvent(event)
        except ValueError as exc:
            raise SafeError("notification_failed", "notification event is unavailable") from exc
        profile_key = _profile_id(profile_obj)
        if not self._enabled(profile_obj, event_obj):
            return False
        now = self.clock()
        if event_obj is NotificationEvent.LOW_DISK:
            previous = self._low_disk_sent.get(profile_key)
            if previous is not None and now - previous < self.low_disk_cooldown_seconds:
                return False
        delivered = False
        for channel in CHANNELS:
            try:
                secret = self._secret(channel)
            except SafeError:
                continue
            if self._already_delivered(profile_key, event_obj.value, int(state_generation), channel):
                continue
            cancelled, post_error = await self._post_async(channel, secret, message)
            if post_error is not None:
                # A caller cancellation wins over a transport exception, but
                # the worker result has been consumed so it cannot become an
                # unhandled task warning.
                if cancelled:
                    raise asyncio.CancelledError
                raise post_error
            self._record_delivery(profile_key, event_obj.value, int(state_generation), channel)
            delivered = True
            if cancelled:
                # The POST completed successfully while cancellation was
                # pending.  Record dedup/cooldown state before propagating it.
                if event_obj is NotificationEvent.LOW_DISK:
                    self._low_disk_sent[profile_key] = now
                raise asyncio.CancelledError
        if delivered and event_obj is NotificationEvent.LOW_DISK:
            self._low_disk_sent[profile_key] = now
        return delivered

    async def _post_async(self, channel: str, secret: str, message: str) -> tuple[bool, BaseException | None]:
        """Run one HTTP POST, draining its worker even when cancelled."""

        worker = asyncio.create_task(asyncio.to_thread(self._post, channel, secret, message))
        cancelled = False
        error: BaseException | None = None
        while True:
            try:
                await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                cancelled = True
                # A second cancellation must not abandon the accepted POST.
                continue
            except BaseException as exc:
                error = exc
                break
        if error is None:
            try:
                worker.result()
            except BaseException as exc:
                error = exc
        return cancelled, error

    def close(self) -> None:
        """Close only a default-owned HTTP client, exactly once."""

        if self._http_client_closed:
            return
        if self._owns_http_client:
            close = getattr(self.http_client, "close", None)
            if callable(close):
                close()
        self._http_client_closed = True

    def get_config(self, profile: Any) -> NotificationConfig:
        profile_obj = self._get_profile(profile)
        targets = []
        for channel in CHANNELS:
            try:
                secret = self._secret(channel)
            except SafeError:
                targets.append(NotificationTarget(channel=channel, configured=False, label=None))
            else:
                # Never return a prefix, hash, URL, or token-derived label.
                self.redactor.registry.add(secret)
                targets.append(NotificationTarget(channel=channel, configured=True, label=channel))
        rules = {
            event: self._enabled(profile_obj, event)
            for event in NotificationEvent
        }
        return NotificationConfig(profile_id=profile_obj.id, targets=tuple(targets), rules=rules)

    def set_rule(self, action: Any, actor: str | None = None, request_id: Any | None = None) -> NotificationConfig:
        profile_obj = self._get_profile(getattr(action, "profile_id", action))
        event = getattr(action, "event", None)
        enabled = getattr(action, "enabled", None)
        try:
            event_obj = event if isinstance(event, NotificationEvent) else NotificationEvent(event)
        except ValueError as exc:
            raise SafeError("notification_failed", "notification event is unavailable") from exc
        profile_events = getattr(profile_obj, "notification_events", frozenset())
        if event_obj not in profile_events and event_obj.value not in profile_events:
            raise SafeError("notification_failed", "notification event is not enabled for this profile")
        connection = _connection(self.database)
        if connection is None:
            raise SafeError("notification_failed", "notification rules are unavailable")
        connection.execute(
            "INSERT INTO notification_rules(profile_id,event,enabled) VALUES(?,?,?) "
            "ON CONFLICT(profile_id,event) DO UPDATE SET enabled=excluded.enabled",
            (_profile_id(profile_obj), event_obj.value, int(bool(enabled))),
        )
        connection.commit()
        return self.get_config(profile_obj)

    def test(self, channel: str, profile: Any, actor: str = "system", request_id: Any | None = None) -> bool:
        profile_obj = self._get_profile(profile)
        profile_key = _profile_id(profile_obj)
        try:
            secret = self._secret(channel)
            self._post(channel, secret, "game-control notification test")
        except SafeError as exc:
            self._audit(actor, "test_notification", profile_key, "failed", exc.code)
            raise
        self._audit(actor, "test_notification", profile_key, "succeeded")
        return True


__all__ = ["NotificationService", "CHANNELS", "DEFAULT_SECRET_DIR", "TIMEOUT_SECONDS"]
