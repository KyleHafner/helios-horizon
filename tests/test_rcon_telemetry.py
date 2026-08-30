import asyncio
import math
import struct

import pytest

from game_control.rcon import RconError
from game_control.rcon_telemetry import (
    PerformanceResult,
    PersistentRconTelemetry,
    PlayerCountResult,
    TelemetryCommand,
    TelemetryErrorCode,
    UnavailableResult,
)


def packet(request_id: int, packet_type: int, text: str = "") -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + text.encode() + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class Reader(asyncio.StreamReader):
    def __init__(self, data: bytes):
        super().__init__()
        self.feed_data(data)
        self.feed_eof()


class Writer:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.waited = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


def make_channel(payloads, **kwargs):
    writers = []
    readers = iter(Reader(payload) for payload in payloads)

    async def opener(host, port, **_):
        assert (host, port) == ("127.0.0.1", 25575)
        writer = Writer()
        writers.append(writer)
        return next(readers), writer

    channel = PersistentRconTelemetry(
        "minecraft-sunlit-cobblemon",
        open_connection=opener,
        password_reader=lambda _: "secret",
        sleep=lambda _delay: asyncio.sleep(0),
        **kwargs,
    )
    return channel, writers


@pytest.mark.asyncio
async def test_serialized_connection_returns_typed_sanitized_results():
    channel, writers = make_channel(
        [packet(1, 2) + packet(2, 0, "There are 2 of a max of 20 players online: alice, bob") + packet(3, 0, "Overall: Mean TPS: 19.95")]
    )
    players, tps = await asyncio.gather(
        channel.execute(TelemetryCommand.PLAYER_COUNT), channel.execute(TelemetryCommand.TPS)
    )
    assert players == PlayerCountResult(2, 20)
    assert tps == PerformanceResult(tps=19.95)
    assert "alice" not in repr(players)
    assert len(writers) == 1 and len(writers[0].writes) == 3


@pytest.mark.asyncio
async def test_malformed_identity_bearing_list_is_redacted():
    channel, _ = make_channel([packet(1, 2) + packet(2, 0, "alice, bob")], max_attempts=1)
    with pytest.raises(RconError) as exc:
        await channel.execute(TelemetryCommand.PLAYER_COUNT)
    assert "alice" not in str(exc.value) and "bob" not in str(exc.value)
    assert channel.health.last_error is TelemetryErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_reconnects_and_clamps_jitter():
    channel, _ = make_channel([packet(1, 2) + packet(2, 0, "There are 0 of a max of 20 players online")], max_attempts=2)
    original = channel._open_connection
    calls = 0
    delays = []

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private detail")
        return await original(*args, **kwargs)

    channel._open_connection = fail_once
    channel._jitter = lambda _: 999.0
    channel._sleep = lambda delay: delays.append(delay) or asyncio.sleep(0)
    assert await channel.execute(TelemetryCommand.PLAYER_COUNT) == PlayerCountResult(0, 20)
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_auth_failure_closes_and_awaits_writer():
    channel, writers = make_channel([packet(-1, 2)], max_attempts=1)
    with pytest.raises(RconError, match="authentication"):
        await channel.execute(TelemetryCommand.PLAYER_COUNT)
    assert writers[0].closed and writers[0].waited


@pytest.mark.asyncio
async def test_response_limit_closes_and_awaits_writer():
    channel, writers = make_channel([packet(1, 2) + packet(2, 0, "12345")], max_response_bytes=4, max_attempts=1)
    with pytest.raises(RconError, match="exceeded limit"):
        await channel.execute(TelemetryCommand.PLAYER_COUNT)
    assert writers[0].closed and writers[0].waited
    assert channel.health.response_limit_failures == 1


@pytest.mark.asyncio
async def test_mid_request_deactivation_is_serialized_and_cleans_up():
    release = asyncio.Event()
    writer = Writer()
    reader = Reader(packet(1, 2) + packet(2, 0, "There are 0 of a max of 20 players online"))

    async def opener(*_args, **_kwargs):
        return reader, writer

    channel = PersistentRconTelemetry("profile", open_connection=opener, password_reader=lambda _: "secret")
    original_request = channel._request_locked

    async def blocked(command):
        await release.wait()
        return await original_request(command)

    channel._request_locked = blocked
    request = asyncio.create_task(channel.execute(TelemetryCommand.PLAYER_COUNT))
    await asyncio.sleep(0)
    deactivate = asyncio.create_task(channel.set_active(False))
    await asyncio.sleep(0)
    assert not deactivate.done()
    release.set()
    assert await request == PlayerCountResult(0, 20)
    await deactivate
    assert writer.closed and writer.waited and not channel.health.connected


@pytest.mark.asyncio
async def test_timeout_and_nonfinite_jitter_are_safe():
    channel, _ = make_channel([], max_attempts=2, jitter=lambda _: math.inf)

    async def refused(*_args, **_kwargs):
        raise OSError("secret response body")

    channel._open_connection = refused
    with pytest.raises(RconError, match="backoff") as exc:
        await channel.execute(TelemetryCommand.PLAYER_COUNT)
    assert "secret response body" not in str(exc.value)
    assert channel.health.last_error is TelemetryErrorCode.BACKOFF
    with pytest.raises(ValueError, match="timeout"):
        PersistentRconTelemetry("profile", timeout=math.nan)


@pytest.mark.asyncio
async def test_inactive_identity_close_health_and_last_success_age():
    now = [10.0]
    channel, writers = make_channel(
        [packet(1, 2) + packet(2, 0, "Overall: Mean tick time: 12.5 ms")], clock=lambda: now[0]
    )
    assert await channel.execute(TelemetryCommand.MSPT) == PerformanceResult(mspt=12.5)
    now[0] = 14.5
    assert channel.health.last_success_age_seconds == 4.5
    await channel.set_active(False)
    assert writers[0].waited
    with pytest.raises(RconError, match="inactive"):
        await channel.execute(TelemetryCommand.PLAYER_COUNT)
    assert channel.health.consecutive_failures == 1
    await channel.set_active(True)
    await channel.set_profile_identity("replacement")


@pytest.mark.asyncio
async def test_performance_command_parses_tps_and_mspt_from_one_wire_response():
    channel, writers = make_channel([
        packet(1, 2) + packet(2, 0, "Overall: Mean tick time: 12.5 ms. Mean TPS: 19.95")
    ])
    assert await channel.execute(TelemetryCommand.PERFORMANCE) == PerformanceResult(tps=19.95, mspt=12.5)
    assert len(writers[0].writes) == 2  # one auth packet, one performance command


@pytest.mark.asyncio
async def test_command_refusal_and_version_explicitly_unavailable():
    channel, writers = make_channel([])
    with pytest.raises(RconError, match="not approved"):
        await channel.execute("list")  # type: ignore[arg-type]
    assert await channel.execute(TelemetryCommand.VERSION) == UnavailableResult()
    assert writers == []


def test_endpoint_refusal():
    with pytest.raises(ValueError, match="endpoint"):
        PersistentRconTelemetry("profile", host="10.0.0.1")
    with pytest.raises(ValueError, match="endpoint"):
        PersistentRconTelemetry("profile", port=25576)
