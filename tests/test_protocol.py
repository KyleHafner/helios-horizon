from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from game_control.models import ProfileId
from game_control.protocol import (
    MAX_REQUEST_BYTES,
    RpcRequest,
    RpcResponse,
    parse_request_line,
)
from game_control.slot import SlotObservation
from game_control.slotd_main import UnixRpcServer, _await_free_slot


def test_rejects_shell_like_profile() -> None:
    with pytest.raises(ValidationError):
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "operator",
                "action": {"kind": "start", "profile_id": "pz-rising;id"},
            }
        )


def test_request_line_is_bounded_and_jsonl() -> None:
    request = {
        "request_id": str(uuid4()),
        "actor": "operator",
        "action": {"kind": "start", "profile_id": "minecraft"},
    }
    response = parse_request_line((__import__("json").dumps(request) + "\n").encode())
    assert response.action.profile_id is ProfileId.MINECRAFT
    with pytest.raises(ValueError, match="request too large"):
        parse_request_line(b"{" + b"x" * MAX_REQUEST_BYTES + b"}\n")


def test_request_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RpcRequest.model_validate(
            {
                "request_id": str(uuid4()),
                "actor": "operator",
                "action": {
                    "kind": "start",
                    "profile_id": "minecraft",
                    "command": "id",
                },
            }
        )


def test_response_round_trips_discriminated_envelope() -> None:
    response = TypeAdapter(RpcResponse).validate_python(
        {
            "request_id": str(uuid4()),
            "ok": False,
            "error": {
                "code": "invalid_request",
                "message": "invalid request",
                "retryable": False,
            },
        }
    )
    assert response.ok is False


def test_peer_credentials_are_pid_uid_gid() -> None:
    class Sock:
        def getsockopt(self, *_args):
            import struct
            return struct.pack("3i", 11, 22, 33)
    assert UnixRpcServer.peer_credentials(Sock()) == (11, 22, 33)


def test_authorize_peer_rejects_unauthorized_uid_and_gid() -> None:
    server = UnixRpcServer(
        object(),
        uid=1000,
        gid=2000,
        primary_gid=1000,
    )
    assert server.authorize_peer(1234, 1234) is False
    assert server.authorize_peer(1000, 2000) is False
    assert server.authorize_peer(1000, 1000) is True


@pytest.mark.asyncio
async def test_await_free_slot_polls_until_inconsistent_slot_releases() -> None:
    class Inspector:
        def __init__(self):
            self.calls = 0

        def observe(self):
            self.calls += 1
            if self.calls < 3:
                return SlotObservation(owner=ProfileId.MINECRAFT, inconsistent=False)
            return SlotObservation(owner=None, inconsistent=False)

    inspector = Inspector()
    assert await _await_free_slot(inspector, 0.1, poll_interval=0) is True
    assert inspector.calls == 3
