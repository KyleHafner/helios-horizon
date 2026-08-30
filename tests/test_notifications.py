from __future__ import annotations

import json
import sqlite3
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from game_control.models import (
    AdapterKind,
    NotificationEvent,
    OperationName,
    PathSpec,
    PortSpec,
    ProcessSpec,
    Profile,
    ProfileId,
    UpdateSpec,
)
from game_control.notifications import NotificationService
from game_control.redaction import Redactor


_OPEN_DATABASES: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def close_test_databases():
    yield
    while _OPEN_DATABASES:
        _OPEN_DATABASES.pop().close()


def _profile(tmp_path: Path) -> Profile:
    data = tmp_path / "data"
    data.mkdir()
    return Profile(
        id=ProfileId.MINECRAFT,
        display_name="Minecraft",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="minecraft.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=25565),),
        start_timeout_seconds=5,
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
        paths=PathSpec(
            data_roots=(data,),
            mutable_root=data,
            backup_root=tmp_path / "backups",
            install_root=data,
            version_file=data / "version",
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.START}),
        update=UpdateSpec(kind="manual"),
        notification_events=frozenset({NotificationEvent.START, NotificationEvent.LOW_DISK}),
    )


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE notification_rules(profile_id TEXT, event TEXT, enabled INTEGER,
                                        PRIMARY KEY(profile_id,event));
        CREATE TABLE notification_deliveries(
          id TEXT PRIMARY KEY, profile_id TEXT, event TEXT, state_generation INTEGER,
          channel TEXT, delivered_at TEXT, error_code TEXT);
        CREATE TABLE audit(
          id TEXT PRIMARY KEY, timestamp TEXT, actor TEXT, action TEXT,
          profile_id TEXT, result TEXT, error_code TEXT, detail TEXT);
        """
    )
    _OPEN_DATABASES.append(db)
    return db


def test_send_uses_fixed_payload_and_timeout_without_leaking_secret(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    webhook = "https://discord.example.invalid/webhook/synthetic-token"
    (secrets / "discord").write_text(webhook)
    (secrets / "discord").chmod(0o600)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    service = NotificationService(
        profiles={profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler), timeout=10.0
        ),
        redactor=Redactor(),
    )
    result = service.send(profile.id, NotificationEvent.START, 3, "server started")

    assert result is True
    assert seen[0].url == webhook
    assert service.timeout_seconds == 10
    assert seen[0].content == b'{"content":"server started"}'
    assert webhook not in str(service.last_error or "")
    assert webhook not in service.redactor.redact(f"failed {webhook}")


def test_falsey_injected_http_client_is_borrowed_and_close_is_idempotent(tmp_path: Path):
    profile = _profile(tmp_path)

    class FalseyClient:
        def __init__(self):
            self.closed = 0

        def __bool__(self):
            return False

        def close(self):
            self.closed += 1

    client = FalseyClient()
    service = NotificationService({profile.id.value: profile}, http_client=client)
    assert service.http_client is client
    assert service._owns_http_client is False
    service.close()
    service.close()
    assert client.closed == 0


def test_default_http_client_is_owned_and_close_is_idempotent(monkeypatch, tmp_path: Path):
    profile = _profile(tmp_path)

    class Client:
        def __init__(self, **_kwargs):
            self.closed = 0

        def close(self):
            self.closed += 1

    client = Client()
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: client)
    service = NotificationService({profile.id.value: profile})
    assert service._owns_http_client is True
    service.close()
    service.close()
    assert client.closed == 1


@pytest.mark.asyncio
async def test_send_async_shield_drains_successful_post_before_reraising_cancel(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    webhook = "https://discord.example.invalid/cancellation-success"
    (secrets / "discord").write_text(webhook)
    (secrets / "discord").chmod(0o600)
    started = threading.Event()
    release = threading.Event()

    class Client:
        def post(self, _url, **_kwargs):
            started.set()
            release.wait(2)

    db = _db()
    service = NotificationService({profile.id.value: profile}, secret_dir=secrets, database=db, http_client=Client())
    task = asyncio.create_task(service.send_async(profile.id, NotificationEvent.START, 33, "started"))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_send_async_cancellation_consumes_failed_post_without_record(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    webhook = "https://discord.example.invalid/cancellation-failure"
    (secrets / "discord").write_text(webhook)
    (secrets / "discord").chmod(0o600)
    started = threading.Event()
    release = threading.Event()

    class Client:
        def post(self, _url, **_kwargs):
            started.set()
            release.wait(2)
            raise RuntimeError(f"failed {webhook}")

    db = _db()
    service = NotificationService({profile.id.value: profile}, secret_dir=secrets, database=db, http_client=Client())
    task = asyncio.create_task(service.send_async(profile.id, NotificationEvent.START, 34, "started"))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 0


def test_profile_event_and_generation_deduplicate_delivery(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "discord").write_text("https://discord.example.invalid/synthetic")
    (secrets / "discord").chmod(0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    service = NotificationService(
        profiles={profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert service.send(profile.id, NotificationEvent.START, 7, "started")
    assert not service.send(profile.id, NotificationEvent.START, 7, "started again")
    assert calls == 1
    assert not service.send(profile.id, NotificationEvent.CRASH, 8, "not enabled")


def test_test_notification_is_audited_and_config_is_masked(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "telegram").write_text("synthetic-telegram-token")
    (secrets / "telegram").chmod(0o600)
    db = _db()
    service = NotificationService(
        profiles={profile.id.value: profile},
        secret_dir=secrets,
        database=db,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200))
        ),
    )

    config = service.get_config(profile.id)
    assert config.targets[0].configured is False
    assert config.targets[1].configured is True
    assert "synthetic" not in repr(config)
    service.test("telegram", profile.id, actor="operator")
    assert db.execute("SELECT action,result FROM audit").fetchone() == (
        "test_notification",
        "succeeded",
    )


def test_low_disk_event_has_cooldown(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "discord").write_text("https://discord.example.invalid/synthetic")
    (secrets / "discord").chmod(0o600)
    service = NotificationService(
        profiles={profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(204))
        ),
        low_disk_cooldown_seconds=60,
        clock=lambda: 100.0,
    )
    assert service.send(profile.id, NotificationEvent.LOW_DISK, 1, "low")
    assert not service.send(profile.id, NotificationEvent.LOW_DISK, 2, "low")


def test_delivery_error_redacts_installed_target(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    webhook = "https://discord.example.invalid/webhook/synthetic-error"
    (secrets / "discord").write_text(webhook)
    (secrets / "discord").chmod(0o600)

    class FailingClient:
        def post(self, url, **_kwargs):
            raise RuntimeError(f"transport failed for {url}")

    service = NotificationService(
        profiles={profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=FailingClient(),
    )
    with pytest.raises(Exception) as exc:
        service.send(profile.id, NotificationEvent.START, 1, "started")
    assert webhook not in str(exc.value)
    assert webhook not in (service.last_error or "")
    assert exc.value.__cause__ is None


@pytest.mark.parametrize("channel", ["discord", "telegram"])
def test_missing_secret_is_safe(tmp_path: Path, channel: str):
    profile = _profile(tmp_path)
    service = NotificationService(
        profiles={profile.id.value: profile}, secret_dir=tmp_path / "secrets", database=_db()
    )
    with pytest.raises(Exception) as exc:
        service.test(channel, profile.id, actor="operator")
    assert "synthetic" not in str(exc.value)


def test_send_delivers_channels_in_fixed_order_with_channel_payloads(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    discord = "https://discord.example.invalid/synthetic"
    (secrets / "discord").write_text(discord)
    (secrets / "discord").chmod(0o600)
    (secrets / "telegram").write_text("synthetic-telegram-token")
    (secrets / "telegram").chmod(0o600)
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(204)

    service = NotificationService(
        {profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        telegram_chat_id="chat-123",
    )

    assert service.send(profile.id, NotificationEvent.START, 11, "started")

    assert seen == [
        (discord, {"content": "started"}),
        ("https://api.telegram.org/botsynthetic-telegram-token/sendMessage", {
            "chat_id": "chat-123",
            "text": "started",
        }),
    ]


def test_disabled_rule_prevents_delivery_and_unknown_event_is_safe(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "discord").write_text("https://discord.example.invalid/synthetic")
    (secrets / "discord").chmod(0o600)
    db = _db()
    db.execute(
        "INSERT INTO notification_rules(profile_id,event,enabled) VALUES(?,?,?)",
        (profile.id.value, NotificationEvent.START.value, 0),
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    service = NotificationService(
        {profile.id.value: profile},
        secret_dir=secrets,
        database=db,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert not service.send(profile.id, NotificationEvent.START, 1, "disabled")
    assert calls == 0
    with pytest.raises(Exception) as exc:
        service.send(profile.id, "hostile-event", 1, "ignored")
    assert "hostile" not in str(exc.value)


def test_delivery_http_failure_is_retryable_and_redacted(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    webhook = "https://discord.example.invalid/webhook/http-error"
    (secrets / "discord").write_text(webhook)
    (secrets / "discord").chmod(0o600)

    class Response:
        def raise_for_status(self):
            raise RuntimeError(f"upstream rejected {webhook}")

    class Client:
        def post(self, _url, **_kwargs):
            return Response()

    service = NotificationService(
        {profile.id.value: profile},
        secret_dir=secrets,
        database=_db(),
        http_client=Client(),
    )

    with pytest.raises(Exception) as exc:
        service.send(profile.id, NotificationEvent.START, 1, "started")

    assert exc.value.code == "notification_failed"
    assert exc.value.retryable is True
    assert webhook not in (service.last_error or "")


def test_invalid_secret_shape_and_symlink_are_not_used(tmp_path: Path):
    profile = _profile(tmp_path)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "telegram").write_text("token/with-path")
    (secrets / "telegram").chmod(0o600)
    external = tmp_path / "external-secret"
    external.write_text("https://discord.example.invalid/synthetic")
    (secrets / "discord").symlink_to(external)
    service = NotificationService({profile.id.value: profile}, secret_dir=secrets, database=_db())

    with pytest.raises(Exception) as telegram_error:
        service.test("telegram", profile.id)
    assert telegram_error.value.code == "notification_unconfigured"
    with pytest.raises(Exception) as discord_error:
        service.test("discord", profile.id)
    assert discord_error.value.code == "notification_unconfigured"


def test_set_rule_persists_enabled_state_and_rejects_unsupported_events(tmp_path: Path):
    profile = _profile(tmp_path)
    db = _db()
    service = NotificationService({profile.id.value: profile}, secret_dir=tmp_path / "secrets", database=db)

    config = service.set_rule(
        SimpleNamespace(profile_id=profile.id, event=NotificationEvent.START, enabled=False),
        actor="operator",
    )

    assert config.rules[NotificationEvent.START] is False
    assert db.execute(
        "SELECT enabled FROM notification_rules WHERE profile_id=? AND event=?",
        (profile.id.value, NotificationEvent.START.value),
    ).fetchone() == (0,)
    with pytest.raises(Exception) as unsupported:
        service.set_rule(SimpleNamespace(profile_id=profile.id, event=NotificationEvent.CRASH, enabled=True))
    assert unsupported.value.code == "notification_failed"
    with pytest.raises(Exception) as malformed:
        service.set_rule(SimpleNamespace(profile_id=profile.id, event="not-an-event", enabled=True))
    assert malformed.value.code == "notification_failed"


def test_set_rule_without_database_and_failed_test_are_audited(tmp_path: Path):
    profile = _profile(tmp_path)
    action = SimpleNamespace(profile_id=profile.id, event=NotificationEvent.START, enabled=True)
    without_db = NotificationService({profile.id.value: profile}, secret_dir=tmp_path / "secrets")
    with pytest.raises(Exception) as no_db_error:
        without_db.set_rule(action)
    assert no_db_error.value.code == "notification_failed"

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "telegram").write_text("token/with-path")
    (secrets / "telegram").chmod(0o600)
    db = _db()
    service = NotificationService({profile.id.value: profile}, secret_dir=secrets, database=db)
    with pytest.raises(Exception):
        service.test("telegram", profile.id, actor="operator")

    assert db.execute("SELECT actor,result,error_code FROM audit").fetchone() == (
        "operator",
        "failed",
        "notification_unconfigured",
    )
