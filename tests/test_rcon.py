import asyncio
import struct
from pathlib import Path

import pytest

from game_control.rcon import RCON_MAX_PASSWORD_BYTES, RconClient, RconError, SunlitRconTransport, _read_password


def _response(request_id: int, packet_type: int, text: str = "") -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + text.encode() + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class _Reader(asyncio.StreamReader):
    def __init__(self, payload: bytes):
        super().__init__()
        self.feed_data(payload)
        self.feed_eof()


class _Writer:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, payload):
        self.writes.append(payload)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


@pytest.mark.asyncio
async def test_rcon_is_loopback_fixed_and_does_not_return_password():
    writer = _Writer()
    reader = _Reader(_response(1, 2) + _response(2, 0, "Executed"))

    async def open_connection(host, port, **_kwargs):
        assert (host, port) == ("127.0.0.1", 25575)
        return reader, writer

    client = RconClient(
        open_connection=open_connection,
        password_reader=lambda _path: "generated-secret",
    )
    output = await client.execute("say hello")

    assert output == "Executed"
    assert writer.closed is True
    assert b"generated-secret" in writer.writes[0]
    assert b"say hello" in writer.writes[1]


@pytest.mark.asyncio
async def test_rcon_accepts_optional_empty_response_value_before_auth_response():
    writer = _Writer()
    reader = _Reader(_response(-1, 0) + _response(1, 2) + _response(2, 0, "Executed"))

    async def open_connection(*_args, **_kwargs):
        return reader, writer

    client = RconClient(
        open_connection=open_connection,
        password_reader=lambda _path: "secret",
    )
    assert await client.execute("say hello") == "Executed"


@pytest.mark.asyncio
async def test_rcon_requires_fixed_ack_for_flush():
    writer = _Writer()
    reader = _Reader(_response(1, 2) + _response(2, 0))

    async def open_connection(*_args, **_kwargs):
        return reader, writer

    transport = SunlitRconTransport(
        RconClient(open_connection=open_connection, password_reader=lambda _path: "secret")
    )
    with pytest.raises(RconError, match="acknowledgement"):
        await transport.save_all_flush()


def test_rcon_rejects_noncanonical_configuration(tmp_path: Path):
    with pytest.raises(ValueError):
        RconClient(host="0.0.0.0")
    with pytest.raises(ValueError):
        RconClient(password_path=tmp_path / "password")


def test_python_rcon_reader_rejects_oversized_root_credential(tmp_path: Path):
    path = tmp_path / "password"
    path.write_bytes(b"x" * (RCON_MAX_PASSWORD_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(RconError, match="credential is unavailable"):
        _read_password(path)


def test_rcon_prepare_helper_rejects_oversized_credential(tmp_path: Path, monkeypatch):
    import importlib.machinery
    import importlib.util

    helper_path = Path(__file__).parents[1] / "ops/bin/game-sunlit-rcon-prepare"
    loader = importlib.machinery.SourceFileLoader("game_sunlit_rcon_prepare", str(helper_path))
    spec = importlib.util.spec_from_file_location("game_sunlit_rcon_prepare", helper_path, loader=loader)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    credential_dir = tmp_path / "credentials"
    credential_dir.mkdir()
    (credential_dir / "minecraft-rcon-password").write_bytes(
        b"x" * (helper.MAX_CREDENTIAL_BYTES + 1)
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_dir))
    with pytest.raises(ValueError, match="credential is unavailable"):
        helper._credential()
