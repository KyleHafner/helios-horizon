from __future__ import annotations

from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from game_control.models import (
    AdapterKind,
    HealthState,
    NotificationEvent,
    OperationName,
    ProfileId,
    UpdateSpec,
)
from game_control.notifications import NotificationService
from game_control.protocol import GetStatus, RpcRequest, TestNotification as _NotificationAction
from game_control.protocol import CreateBackup
import game_control.service_wiring as wiring
from game_control.service_wiring import _BackupFacade, _NotificationFacade
from game_control import slotd_main


class _Adapter:
    async def observe(self, profile):
        return SimpleNamespace(
            running=False,
            healthy=None,
            pid=None,
            started_at=None,
            required_ports_ready=False,
        )

    async def recent_logs(self, profile, limit):
        return []


class _Registry:
    def __init__(self, profiles):
        self.profiles = tuple(profiles)

    def __iter__(self):
        return iter(self.profiles)


def _profile(profile_id: ProfileId, adapter: AdapterKind):
    return SimpleNamespace(
        id=profile_id,
        display_name=profile_id.value,
        adapter=adapter,
        process=SimpleNamespace(executable="/usr/bin/game", argv_contains=()),
        ports=(SimpleNamespace(protocol="tcp", port=25565, required=True),),
        paths=SimpleNamespace(
            data_roots=("/var/lib/game",),
            mutable_root="/var/lib/game",
            log_files=(),
            backup_root="/var/backups/game",
            install_root="/opt/game",
            version_file="/var/lib/game/version",
        ),
        operations=frozenset(OperationName),
        update=SimpleNamespace(kind="manual"),
        notification_events=frozenset(),
        health_timeout_seconds=5,
    )


class _State:
    def __init__(self):
        self.connection = SimpleNamespace()


@pytest.mark.asyncio
async def test_build_controller_wires_real_typed_service_seams(monkeypatch, tmp_path):
    profiles = [
        _profile(ProfileId.MINECRAFT, AdapterKind.CRAFTY),
        _profile(ProfileId.PZ_RISING, AdapterKind.SYSTEMD),
        _profile(ProfileId.TERRARIA_VANILLA, AdapterKind.SYSTEMD),
        _profile(ProfileId.TERRARIA_TMOD, AdapterKind.SYSTEMD),
    ]
    registry = _Registry(profiles)
    monkeypatch.setattr(slotd_main, "ProfileRegistry", SimpleNamespace(load=lambda path: registry))
    monkeypatch.setattr(slotd_main, "StateDatabase", SimpleNamespace(open=lambda path: _State()))
    monkeypatch.setattr(slotd_main, "CraftyAdapter", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(slotd_main, "SystemdAdapter", lambda *args, **kwargs: _Adapter())
    monkeypatch.setattr(
        slotd_main,
        "SlotInspector",
        lambda: SimpleNamespace(observe=lambda: SimpleNamespace(owner=None, inconsistent=False)),
    )
    config = tmp_path / "controller.toml"
    config.write_text("[crafty]\nbase_url='https://127.0.0.1:8443'\ntoken_path='/dev/null'\n")

    controller = slotd_main.build_controller(config)
    assert controller.services.status is not None
    assert controller.services.logs is not None
    assert controller.services.backups is not None
    assert controller.services.updates is not None
    assert controller.services.notifications is not None
    assert controller.services.audit is not None

    snapshot = await controller.services.status.snapshot(GetStatus(kind="get_status"))
    assert len(snapshot.profiles) == len(profiles)
    assert {item.profile_id for item in snapshot.profiles} == {profile.id for profile in profiles}
    assert all(item.health is HealthState.UNKNOWN for item in snapshot.profiles)


@pytest.mark.asyncio
async def test_notification_facade_keeps_sqlite_audit_on_event_loop(tmp_path):
    import sqlite3
    import threading

    profile = _profile(ProfileId.MINECRAFT, AdapterKind.CRAFTY)
    profile.notification_events = frozenset({NotificationEvent.START})
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE audit(id TEXT PRIMARY KEY,timestamp TEXT,actor TEXT,action TEXT,"
        "profile_id TEXT,result TEXT,error_code TEXT,detail TEXT)"
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret = secret_dir / "discord"
    secret.write_text("https://discordapp.com/api/webhooks/123/token")
    secret.chmod(0o600)
    service = NotificationService(
        {ProfileId.MINECRAFT.value: profile},
        secret_dir=secret_dir,
        database=connection,
    )
    service._post = lambda channel, value, message: None
    facade = _NotificationFacade(service)

    result = await facade.test(
        _NotificationAction(kind="test_notification", channel="discord", profile_id=ProfileId.MINECRAFT),
        actor="operator",
    )
    assert result.state == "running"
    assert connection.execute("SELECT count(*) FROM audit").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_notification_facade_send_keeps_sqlite_on_event_loop(tmp_path):
    import sqlite3

    profile = _profile(ProfileId.MINECRAFT, AdapterKind.CRAFTY)
    profile.notification_events = frozenset({NotificationEvent.START})
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE notification_rules(profile_id TEXT, event TEXT, enabled INTEGER,
                                        PRIMARY KEY(profile_id,event));
        CREATE TABLE notification_deliveries(
          id TEXT PRIMARY KEY, profile_id TEXT, event TEXT, state_generation INTEGER,
          channel TEXT, delivered_at TEXT, error_code TEXT);
        """
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret = secret_dir / "discord"
    secret.write_text("https://discordapp.com/api/webhooks/123/token")
    secret.chmod(0o600)
    service = NotificationService(
        {ProfileId.MINECRAFT.value: profile},
        secret_dir=secret_dir,
        database=connection,
    )
    main_thread = threading.get_ident()
    post_threads = []
    service._post = lambda channel, value, message: post_threads.append(threading.get_ident())
    facade = _NotificationFacade(service)

    assert await facade.send(ProfileId.MINECRAFT, NotificationEvent.START, 9, "started")
    assert post_threads and post_threads[0] != main_thread
    assert connection.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_backup_state_database_opens_inside_worker(monkeypatch):
    profile = _profile(ProfileId.MINECRAFT, AdapterKind.CRAFTY)
    adapter = _Adapter()
    main_thread = threading.get_ident()
    opened_on = []

    class _FakeBackup:
        def __init__(self, profile, *, database, **kwargs):
            self.database = database
            self.free_space = lambda _path: 1
            self.clock = lambda: datetime.now(timezone.utc)
            self.tar_runner = lambda *args, **kwargs: None

        def create(self, *args, **kwargs):
            return SimpleNamespace(id="backup-worker")

        def _insert(self, record):
            raise AssertionError("isolated worker should persist directly")

    def isolated(_database):
        opened_on.append(threading.get_ident())
        return object()

    monkeypatch.setattr(wiring, "BackupService", _FakeBackup)
    monkeypatch.setattr(wiring, "_isolated_database", isolated)
    monkeypatch.setattr(wiring, "_close_database", lambda _database: None)
    facade = _BackupFacade(
        {ProfileId.MINECRAFT.value: profile},
        {ProfileId.MINECRAFT.value: adapter},
        object(),
    )
    result = await facade.create(CreateBackup(kind="create_backup", profile_id=ProfileId.MINECRAFT))
    assert result.job_id == "backup-worker"
    assert opened_on and opened_on[0] != main_thread
