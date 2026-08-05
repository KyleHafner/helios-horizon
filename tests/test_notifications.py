from __future__ import annotations

import sqlite3
from pathlib import Path

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
