import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from game_control.adapters.base import AdapterError
from game_control.adapters.crafty import CraftyAdapter
from game_control.adapters.systemd import SystemdAdapter
from game_control.api import ROUTE_ACTIONS
from game_control.controller import Controller
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
from game_control.protocol import (
    Command,
    ErrorCode,
    RpcProvenance,
    RpcFailure,
    RpcRequest,
)
from game_control.web_main import create_app


def _profile(
    adapter: AdapterKind = AdapterKind.SYSTEMD,
    profile_id: ProfileId = ProfileId.MINECRAFT,
    operations: frozenset[OperationName] | None = None,
) -> Profile:
    values = {
        "id": profile_id,
        "display_name": "Minecraft",
        "adapter": adapter,
        "process": ProcessSpec(executable=Path("/usr/bin/java")),
        "ports": (PortSpec(protocol="tcp", port=25565),),
        "start_timeout_seconds": 5,
        "stop_timeout_seconds": 5,
        "health_timeout_seconds": 5,
        "paths": PathSpec(
            data_roots=(Path("/var/lib/game-control/minecraft"),),
            mutable_root=Path("/var/lib/game-control/minecraft"),
            backup_root=Path("/var/backups/game-control/minecraft"),
            install_root=Path("/opt/game-control/minecraft"),
            version_file=Path("/var/lib/game-control/minecraft/version"),
        ),
        "min_available_memory_bytes": 1,
        "min_free_disk_bytes": 1,
        "operations": operations or frozenset({OperationName.START}),
        "update": UpdateSpec(kind="manual"),
    }
    if adapter is AdapterKind.SYSTEMD:
        values["systemd_unit"] = "minecraft.service"
    else:
        values["crafty_server_id"] = uuid4()
    return Profile(**values)


def _mutation_client(rpc):
    client = TestClient(
        create_app(rpc=rpc, proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    auth = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
    session = client.get("/api/v1/session", headers=auth)
    csrf = session.json()["csrf_token"]
    return client, {**auth, "X-CSRF-Token": csrf, "Origin": "https://games.example.com"}


def test_command_action_is_strict_and_normalizes_only_outer_spaces():
    action = Command(kind="command", profile_id="minecraft", command="  say hello  ")
    assert action.command == "say hello"
    with pytest.raises(ValidationError):
        Command.model_validate({"kind": "command", "profile_id": "minecraft", "command": "say", "extra": 1})


@pytest.mark.parametrize(
    "command",
    ["", " \t ", "x\n", "x\x00", "x\x1f", "x\x7f", "x" * 513],
)
def test_command_action_rejects_empty_control_and_oversize(command):
    with pytest.raises(ValidationError):
        Command(kind="command", profile_id="minecraft", command=command)


def test_command_route_requires_csrf_origin_and_session():
    client = TestClient(
        create_app(proxy_credential="secret", session_db=":memory:"),
        base_url="https://games.example.com",
    )
    auth = {"X-Game-Control-Proxy": "secret", "X-authentik-username": "operator"}
    missing_session = client.post("/api/v1/profiles/minecraft/command", json={"command": "say hi"}, headers=auth)
    assert missing_session.status_code == 401

    session = client.get("/api/v1/session", headers=auth)
    csrf = session.json()["csrf_token"]
    missing_origin = client.post(
        "/api/v1/profiles/minecraft/command",
        json={"command": "say hi"},
        headers={**auth, "X-CSRF-Token": csrf},
    )
    assert missing_origin.status_code == 403


def test_command_route_forbids_extra_fields_and_does_not_forward_invalid_input():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        return RpcFailure(
            request_id=uuid4(),
            error={
                "code": ErrorCode.INVALID_REQUEST,
                "message": "command unsupported",
                "retryable": False,
            },
        )

    client, headers = _mutation_client(rpc)
    response = client.post(
        "/api/v1/profiles/minecraft/command",
        json={"command": "say hi", "path": "/tmp/escape"},
        headers=headers,
    )
    assert response.status_code == 422
    assert calls == []


def test_http_command_cannot_select_provenance_or_actor_fields():
    calls = []

    async def rpc(request):
        calls.append(request)
        return RpcFailure(
            request_id=request.request_id,
            error={"code": ErrorCode.INVALID_REQUEST, "message": "command unsupported", "retryable": False},
        )

    client, headers = _mutation_client(rpc)
    response = client.post(
        "/api/v1/profiles/minecraft/command",
        json={"command": "say hi", "actor": "root", "provenance": "web-human"},
        headers=headers,
    )
    assert response.status_code == 422
    assert calls == []

    response = client.post(
        "/api/v1/profiles/minecraft/command",
        json={"command": "say hi"},
        headers={**headers, "X-Game-Control-Actor": "root", "X-Game-Control-Provenance": "web-human"},
    )
    assert response.status_code == 400
    assert calls and calls[-1].actor == "operator"
    assert calls[-1].provenance is RpcProvenance.WEB_HUMAN


def test_command_route_normalizes_command_and_never_returns_command_text():
    seen = []

    async def rpc(actor, action):
        seen.append(action)
        return RpcFailure(
            request_id=uuid4(),
            error={
                "code": ErrorCode.INVALID_REQUEST,
                "message": "command unsupported",
                "retryable": False,
            },
        )

    client, headers = _mutation_client(rpc)
    command = "  say secret-console-text  "
    response = client.post(
        "/api/v1/profiles/minecraft/command",
        json={"command": command},
        headers=headers,
    )
    assert response.status_code == 400
    assert seen and seen[0].command == command.strip()
    assert command not in response.text


def test_command_route_is_closed_to_known_profiles():
    assert ROUTE_ACTIONS["POST /api/v1/profiles/{profile_id}/command"] is Command


def test_command_route_rejects_unknown_profile_over_http_without_rpc_dispatch():
    calls = []

    async def rpc(actor, action):
        calls.append(action)
        raise AssertionError("unknown profile reached RPC dispatch")

    client, headers = _mutation_client(rpc)
    response = client.post(
        "/api/v1/profiles/not-a-profile/command",
        json={"command": "say hi"},
        headers=headers,
    )
    assert 400 <= response.status_code < 500
    assert calls == []


@pytest.mark.asyncio
async def test_systemd_command_is_explicitly_unsupported_without_process_spawn(monkeypatch):
    adapter = SystemdAdapter()

    async def fail_spawn(*_args, **_kwargs):
        raise AssertionError("command input reached process transport")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(AdapterError, match="unsupported"):
        await adapter.send_command(_profile(), "say hi")


@pytest.mark.asyncio
async def test_sunlit_command_uses_controller_owned_rcon_transport():
    calls = []

    class Rcon:
        async def execute(self, command):
            calls.append(command)

    await SystemdAdapter(rcon=Rcon()).send_command(
        _profile(
            profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
            operations=frozenset({OperationName.COMMAND}),
        ),
        "say hi",
    )
    assert calls == ["say hi"]


@pytest.mark.asyncio
async def test_crafty_command_uses_verified_stdin_route():
    class Client:
        class Response:
            def raise_for_status(self): pass

        async def request(self, *_args, **_kwargs):
            return self.Response()

    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    await adapter.send_command(_profile(AdapterKind.CRAFTY), "say hi")


@pytest.mark.asyncio
async def test_controller_returns_safe_unsupported_failure_and_redacts_command(tmp_path):
    profile = _profile(operations=frozenset({OperationName.START, OperationName.COMMAND}))
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: SystemdAdapter()},
        operation_lock_factory=lambda: type("Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *_: False})(),
    )
    command = "say secret-controller-text"
    response = await controller.execute(
        RpcRequest(
            request_id=uuid4(),
            actor="operator",
            action=Command(kind="command", profile_id=ProfileId.MINECRAFT, command=command),
        )
    )
    assert isinstance(response, RpcFailure)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    dumped = response.model_dump_json()
    assert command not in dumped
    canonical = controller._db().execute("SELECT canonical_request FROM rpc_idempotency").fetchone()[0]
    assert command not in canonical
    audit = controller._db().execute(
        "SELECT action, result, error_code FROM audit WHERE result='failed'"
    ).fetchone()
    assert audit == ("command", "failed", "invalid_request")
    audit_detail = controller._db().execute("SELECT detail FROM audit").fetchone()[0]
    assert command not in audit_detail


@pytest.mark.asyncio
async def test_direct_rpc_cannot_execute_sunlit_command_as_a_service_actor():
    profile = _profile(
        profile_id=ProfileId.MINECRAFT_SUNLIT_COBBLEMON,
        operations=frozenset({OperationName.COMMAND}),
    )
    calls = []

    class Rcon:
        async def execute(self, command):
            calls.append(command)

    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: SystemdAdapter(rcon=Rcon())},
        operation_lock_factory=lambda: type(
            "Lock", (), {"__enter__": lambda self: self, "__exit__": lambda self, *_: False}
        )(),
    )
    response = await controller.execute(
        RpcRequest(
            request_id=uuid4(),
            actor="operator",
            action=Command(kind="command", profile_id=profile.id, command="say hi"),
        )
    )
    assert isinstance(response, RpcFailure)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert response.error.message == "authenticated browser command required"
    assert calls == []
    assert controller._db().execute("SELECT actor FROM audit").fetchone()[0] == "operator"


@pytest.mark.asyncio
async def test_controller_rejects_command_when_profile_does_not_declare_operation(monkeypatch):
    profile = _profile(profile_id=ProfileId.TERRARIA_TMOD_145_CANDIDATE)
    adapter = SystemdAdapter()

    async def fail_send(*_args, **_kwargs):
        raise AssertionError("undeclared command operation reached adapter")

    monkeypatch.setattr(adapter, "send_command", fail_send)
    controller = Controller(
        profiles={profile.id: profile},
        adapters={profile.id: adapter},
        operation_lock_factory=lambda: type(
            "Lock",
            (),
            {"__enter__": lambda self: self, "__exit__": lambda self, *_: False},
        )(),
    )
    response = await controller.execute(
        RpcRequest(
            request_id=uuid4(),
            actor="operator",
            action=Command(
                kind="command",
                profile_id=profile.id,
                command="save",
            ),
        )
    )

    assert isinstance(response, RpcFailure)
    assert response.error.code is ErrorCode.INVALID_REQUEST
    assert response.error.message == "operation is not permitted for profile"
    assert controller._db().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_exception",
    [
        RuntimeError("adapter secret /tmp/command.fifo save"),
        OSError("adapter secret /tmp/command.fifo save"),
    ],
)
async def test_controller_sanitizes_unexpected_command_adapter_failure(adapter_exception, monkeypatch):
    profile = _profile(operations=frozenset({OperationName.START, OperationName.COMMAND}))
    adapter = SystemdAdapter()

    async def failed_send_command(*_args, **_kwargs):
        raise adapter_exception

    monkeypatch.setattr(adapter, "send_command", failed_send_command)
    controller = Controller(
        profiles={ProfileId.MINECRAFT: profile},
        adapters={ProfileId.MINECRAFT: adapter},
        operation_lock_factory=lambda: type(
            "Lock",
            (),
            {"__enter__": lambda self: self, "__exit__": lambda self, *_: False},
        )(),
    )
    response = await controller.execute(
        RpcRequest(
            request_id=uuid4(),
            actor="operator",
            action=Command(
                kind="command",
                profile_id=ProfileId.MINECRAFT,
                command="save",
            ),
        )
    )

    assert response.error.code is ErrorCode.INTERNAL_ERROR
    assert response.error.message == "command failed"
    dumped = response.model_dump_json()
    assert "adapter secret" not in dumped
    assert "/tmp/command.fifo" not in dumped
    assert "save" not in dumped
    job = controller._db().execute(
        "SELECT state, detail FROM jobs"
    ).fetchone()
    assert job == ("failed", "command failed")
    audit = controller._db().execute(
        "SELECT result, error_code, detail FROM audit WHERE result='failed'"
    ).fetchone()
    assert audit == ("failed", "internal_error", "command failed")
    audit_detail = controller._db().execute("SELECT detail FROM audit").fetchone()[0]
    assert "adapter secret" not in audit_detail
    assert "/tmp/command.fifo" not in audit_detail
    assert "save" not in audit_detail
