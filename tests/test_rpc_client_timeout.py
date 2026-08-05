from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from game_control import web_main
from game_control.models import ProfileId
from game_control.protocol import ErrorCode, GetStatus, RpcRequest, Start, failure, response_json


class _Reader:
    async def readline(self):
        return response_json(failure(uuid4(), ErrorCode.INTERNAL_ERROR, "unavailable"))


class _BlockingReader:
    async def readline(self):
        await asyncio.Event().wait()


class _Writer:
    def __init__(self):
        self.closed = False
        self.wait_closed_called = False

    def write(self, data):
        return None

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.wait_closed_called = True
        return None


def _request(action):
    return RpcRequest(request_id=uuid4(), actor="operator", action=action)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (GetStatus(kind="get_status"), 5.0),
        (Start(kind="start", profile_id=ProfileId.MINECRAFT), 300.0),
    ],
)
async def test_rpc_timeout_is_closed_by_action_kind(monkeypatch, action, expected):
    writer = _Writer()
    observed = []

    async def open_connection(_path):
        return _Reader(), writer

    async def wait_for(awaitable, timeout):
        observed.append(timeout)
        return await awaitable

    monkeypatch.setattr(web_main.asyncio, "open_unix_connection", open_connection)
    monkeypatch.setattr(web_main.asyncio, "wait_for", wait_for)
    client = web_main.UnixRpcClient(web_main.CONTROL_SOCKET)
    response = await client(_request(action))
    assert response.ok is False
    assert observed == [expected, expected]
    assert writer.closed


@pytest.mark.asyncio
async def test_rpc_cancellation_closes_socket(monkeypatch):
    writer = _Writer()

    async def open_connection(_path):
        return _BlockingReader(), writer

    monkeypatch.setattr(web_main.asyncio, "open_unix_connection", open_connection)
    client = web_main.UnixRpcClient(web_main.CONTROL_SOCKET)
    task = asyncio.create_task(client(_request(GetStatus(kind="get_status"))))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
    assert writer.wait_closed_called
