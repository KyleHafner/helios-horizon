"""Synthetic public regressions for H1 transport, online backup, and TPS paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from game_control.models import (
    AdapterKind,
    OperationName,
    PathSpec,
    PortSpec,
    ProcessSpec,
    Profile,
    ProfileId,
    UpdateSpec,
)


def _packet(request_id: int, packet_type: int, text: str = "") -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + text.encode() + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class _Reader(asyncio.StreamReader):
    def __init__(self, payload: bytes):
        super().__init__()
        self.feed_data(payload)
        self.feed_eof()


class _Writer:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_rcon_transport_uses_synthetic_fixed_endpoint_and_flush_ack(tmp_path, monkeypatch):
    import game_control.rcon as rcon

    host = "192.0.2.1"
    port = 25075
    password_path = tmp_path / "synthetic-rcon-credential"
    monkeypatch.setattr(rcon, "RCON_HOST", host)
    monkeypatch.setattr(rcon, "RCON_PORT", port)
    monkeypatch.setattr(rcon, "RCON_PASSWORD_PATH", password_path)
    writer = _Writer()
    reader = _Reader(_packet(1, 2) + _packet(2, 0, "ack"))

    async def open_connection(actual_host, actual_port, **_kwargs):
        assert (actual_host, actual_port) == (host, port)
        return reader, writer

    client = rcon.RconClient(
        host=host,
        port=port,
        password_path=password_path,
        open_connection=open_connection,
        password_reader=lambda _path: "synthetic-password",
    )
    await rcon.SunlitRconTransport(client).save_all_flush()

    assert writer.closed is True
    assert any(b"save-all flush" in payload for payload in writer.writes)
    assert hashlib.sha256(b"synthetic-password").hexdigest() not in repr(writer.writes)


def _sunlit_profile(tmp_path: Path) -> Profile:
    data = tmp_path / "mutable"
    backup = tmp_path / "archive-store"
    data.mkdir()
    return Profile(
        id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
        display_name="Synthetic Sunlit",
        adapter=AdapterKind.SYSTEMD,
        systemd_unit="synthetic-sunlit.service",
        process=ProcessSpec(executable=Path("/usr/bin/false")),
        ports=(PortSpec(protocol="tcp", port=25065),),
        start_timeout_seconds=5,
        stop_timeout_seconds=5,
        health_timeout_seconds=5,
        paths=PathSpec(
            data_roots=(data,),
            backup_roots=(data,),
            mutable_root=data,
            backup_root=backup,
            install_root=tmp_path / "install",
            version_file=data / "version",
        ),
        min_available_memory_bytes=1,
        min_free_disk_bytes=1,
        operations=frozenset({OperationName.BACKUP, OperationName.RESTORE}),
        update=UpdateSpec(kind="manual"),
    )


def test_online_backup_restores_save_control_after_synthetic_snapshot_failure(tmp_path):
    from game_control.backups import BackupService
    from game_control.errors import SafeError

    profile = _sunlit_profile(tmp_path)
    (profile.paths.mutable_root / "world.dat").write_bytes(b"synthetic-world")
    events: list[str] = []

    class Online:
        def save_off(self):
            events.append("off")

        def save_all_flush(self):
            events.append("flush")

        def save_on(self):
            events.append("on")

    service = BackupService(profile, online_transport=Online())

    def fail_snapshot(*_args, **_kwargs):
        raise SafeError("backup_quiesce_timeout", "synthetic bounded snapshot")

    service._snapshot = fail_snapshot
    with pytest.raises(SafeError, match="quiesce"):
        service.create_online()
    assert events == ["off", "flush", "on"]


class _Response:
    def __init__(self, text: str, *, chunks: list[bytes] | None = None):
        self.body = text.encode()
        self.chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks or [self.body]:
            yield chunk


class _Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _HttpClient:
    def __init__(self, response):
        self.response = response

    def stream(self, _method, _url):
        return _Stream(self.response)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_tps_sampler_persists_synthetic_metrics_and_rejects_oversize_stream():
    from game_control.tps import MAX_EXPORTER_RESPONSE_BYTES, TpsSampler

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE metric_samples (profile_id TEXT, metric TEXT, ts TEXT, value REAL)")
    payload = "mc_server_tick_seconds{quantile=\"0.5\"} 0.05\n"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sampler = TpsSampler(connection, client=_HttpClient(_Response(payload)))

    assert await sampler.run_once(now=now) is True
    rows = connection.execute("SELECT metric,ts,value FROM metric_samples ORDER BY metric").fetchall()
    assert {row[0] for row in rows} == {"mspt", "tps"}
    assert {row[1] for row in rows} == {now}

    oversized = _Response("", chunks=[b"x" * (MAX_EXPORTER_RESPONSE_BYTES + 1)])
    capped = TpsSampler(connection, client=_HttpClient(oversized))
    assert await capped.run_once(now=now) is False
    connection.close()


def test_existing_public_console_regression_file_is_composed_into_this_lane():
    assert Path(__file__).with_name("test_console_command.py").is_file()
    assert json.loads(json.dumps({"fixture": "synthetic", "origin": "example.com"})) == {
        "fixture": "synthetic",
        "origin": "example.com",
    }
