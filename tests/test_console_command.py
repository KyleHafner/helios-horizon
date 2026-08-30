from __future__ import annotations

import importlib.util
import importlib.machinery
import io
import os
from types import SimpleNamespace
import stat
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from game_control.adapters.base import AdapterError
from game_control.adapters.systemd import SystemdAdapter
from game_control.controller import Controller
from game_control.models import AdapterKind, OperationName, ProfileId
from ops.install import Installer
from game_control.protocol import Command, RpcRequest


ROOT = Path(__file__).parents[1]
HELPER_PATH = ROOT / "ops" / "bin" / "game-console-command"


def _helper_module():
    loader = importlib.machinery.SourceFileLoader("game_console_command", str(HELPER_PATH))
    spec = importlib.util.spec_from_file_location("game_console_command", HELPER_PATH, loader=loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fifo(module, directory: Path, profile: str = "terraria-vanilla") -> Path:
    path = directory / f"{profile}.console"
    os.mkfifo(path, 0o600)
    return path


def _test_directory(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("GAME_CONSOLE_COMMAND_DIR", str(directory))
    monkeypatch.setenv("GAME_CONSOLE_COMMAND_TEST_MODE", "1")


def _read_once(path: Path, result: list[bytes]) -> tuple[threading.Thread, threading.Event]:
    ready = threading.Event()
    release_keeper = threading.Event()

    def reader() -> None:
        # Keep a writer open only long enough to let the real reader attach.
        # Readiness must mean that this fd is open; an O_RDONLY keeper alone
        # lets the helper write before the second open has completed.
        keeper_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        fd = os.open(path, os.O_RDONLY)
        ready.set()
        release_keeper.wait()
        os.close(keeper_fd)
        try:
            result.append(os.read(fd, 32))
        finally:
            os.close(fd)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    assert ready.wait(timeout=1)
    return thread, release_keeper


@pytest.mark.parametrize("command", ["save", "say Server restart in 5 minutes", "time noon"])
def test_helper_writes_exact_command_payload_to_fixed_fifo(tmp_path, monkeypatch, command):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)
    path = _fifo(module, tmp_path)
    received: list[bytes] = []
    reader, release_keeper = _read_once(path, received)

    assert module.main(["terraria-vanilla"], io.BytesIO(command.encode())) == 0
    release_keeper.set()
    reader.join(timeout=1)
    assert received == [command.encode() + b"\n"]


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to emulate service UIDs")
@pytest.mark.parametrize(
    ("profile", "service_user"),
    [
        ("terraria-vanilla", "terraria-vanilla"),
        ("terraria-tmod", "tmodloader"),
        ("terraria-tmod-145-candidate", "tmodloader"),
    ],
)
def test_helper_accepts_mapped_nonroot_owner_and_writes_exact_payload(
    tmp_path, monkeypatch, profile, service_user
):
    module = _helper_module()
    monkeypatch.setattr(module, "DEFAULT_CONSOLE_DIR", tmp_path)
    mapped_uid = 4242
    lookups: list[str] = []
    monkeypatch.setattr(
        module,
        "pwd",
        SimpleNamespace(
            getpwnam=lambda name: (lookups.append(name) or SimpleNamespace(pw_uid=mapped_uid))
        ),
        raising=False,
    )
    path = _fifo(module, tmp_path, profile)
    os.chown(path, mapped_uid, -1)
    received: list[bytes] = []
    reader, release_keeper = _read_once(path, received)

    assert module.main([profile], io.BytesIO("save".encode())) == 0
    release_keeper.set()
    reader.join(timeout=1)
    assert lookups == [service_user]
    assert received == [b"save\n"]


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to emulate service UIDs")
@pytest.mark.parametrize("owner_uid", [0, 4343])
def test_helper_rejects_root_or_other_owned_fifo_for_mapped_profile(
    tmp_path, monkeypatch, owner_uid
):
    module = _helper_module()
    monkeypatch.setattr(module, "DEFAULT_CONSOLE_DIR", tmp_path)
    monkeypatch.setattr(
        module,
        "pwd",
        SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=4242)),
        raising=False,
    )
    path = _fifo(module, tmp_path)
    os.chown(path, owner_uid, -1)

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0


def test_helper_missing_mapped_service_user_fails_closed(tmp_path, monkeypatch):
    module = _helper_module()
    monkeypatch.setattr(module, "DEFAULT_CONSOLE_DIR", tmp_path)
    lookups: list[str] = []

    def missing_user(name: str):
        lookups.append(name)
        raise KeyError(name)

    monkeypatch.setattr(
        module, "pwd", SimpleNamespace(getpwnam=missing_user), raising=False
    )
    _fifo(module, tmp_path)
    monkeypatch.setattr(
        module.os, "open", lambda *_args, **_kwargs: pytest.fail("opened before user lookup")
    )

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0
    assert lookups == ["terraria-vanilla"]


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to emulate service UIDs")
def test_helper_rejects_mapped_owner_change_after_open(tmp_path, monkeypatch):
    module = _helper_module()
    monkeypatch.setattr(module, "DEFAULT_CONSOLE_DIR", tmp_path)
    mapped_uid = 4242
    monkeypatch.setattr(
        module,
        "pwd",
        SimpleNamespace(getpwnam=lambda _name: SimpleNamespace(pw_uid=mapped_uid)),
        raising=False,
    )
    path = _fifo(module, tmp_path)
    os.chown(path, mapped_uid, -1)
    received: list[bytes] = []
    reader, release_keeper = _read_once(path, received)
    real_open = module.os.open

    def open_and_change_owner(path_arg, flags, *args):
        fd = real_open(path_arg, flags, *args)
        os.fchown(fd, 4343, -1)
        return fd

    monkeypatch.setattr(module.os, "open", open_and_change_owner)

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0
    release_keeper.set()
    reader.join(timeout=1)
    assert received == [b""]


@pytest.mark.parametrize(
    ("profile", "command"),
    [
        ("not-a-profile", "save"),
        ("terraria-vanilla", ""),
        ("terraria-tmod", "save\nexit"),
        ("terraria-tmod", "save\rexit"),
        ("terraria-tmod", "say \x00oops"),
        ("terraria-tmod", "x" * 513),
    ],
)
def test_helper_rejects_unknown_profile_or_unsafe_command(tmp_path, monkeypatch, profile, command):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)

    assert module.main([profile], io.BytesIO(command.encode())) != 0


def test_helper_rejects_missing_and_no_reader_fifo(tmp_path, monkeypatch):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0
    _fifo(module, tmp_path)
    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0


@pytest.mark.parametrize("kind", ["symlink", "regular"])
def test_helper_rejects_symlink_and_regular_file(tmp_path, monkeypatch, kind):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)
    path = tmp_path / "terraria-vanilla.console"
    if kind == "symlink":
        target = tmp_path / "target.console"
        os.mkfifo(target, 0o600)
        path.symlink_to(target)
    else:
        path.write_bytes(b"")
        path.chmod(0o600)

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0


def test_helper_rejects_mode_owner_and_link_mismatch(tmp_path, monkeypatch):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)

    mode_path = _fifo(module, tmp_path)
    mode_path.chmod(0o620)
    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0

    mode_path.unlink()
    owner_path = _fifo(module, tmp_path)
    with monkeypatch.context() as owner_patch:
        owner_patch.setattr(module.os, "geteuid", lambda: 4242)
        assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0
    owner_path.unlink()

    link_path = _fifo(module, tmp_path)
    duplicate = tmp_path / "duplicate.console"
    try:
        os.link(link_path, duplicate)
    except OSError as exc:
        pytest.fail(f"hard links to FIFOs must be testable: {exc}")
    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0


def test_helper_rejects_partial_write(tmp_path, monkeypatch):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)
    path = _fifo(module, tmp_path)
    received: list[bytes] = []
    reader, release_keeper = _read_once(path, received)
    monkeypatch.setattr(module.os, "write", lambda _fd, _payload: 1)

    assert module.main(["terraria-vanilla"], io.BytesIO("save".encode())) != 0
    release_keeper.set()
    reader.join(timeout=1)


@pytest.mark.parametrize("stdin", [b"", b"\xff", b"x" * 4097])
def test_helper_rejects_missing_malformed_or_oversized_stdin(tmp_path, monkeypatch, stdin):
    module = _helper_module()
    _test_directory(monkeypatch, tmp_path)
    assert module.main(["terraria-vanilla"], io.BytesIO(stdin)) != 0


def test_helper_subprocess_accepts_test_directory_override(tmp_path):
    path = _fifo(_helper_module(), tmp_path)
    received: list[bytes] = []
    reader, release_keeper = _read_once(path, received)
    env = {
        **os.environ,
        "GAME_CONSOLE_COMMAND_DIR": str(tmp_path),
        "GAME_CONSOLE_COMMAND_TEST_MODE": "1",
    }

    result = subprocess.run(
        [str(HELPER_PATH), "terraria-vanilla"],
        env=env,
        input=b"save",
        capture_output=True,
        check=False,
    )
    release_keeper.set()
    reader.join(timeout=1)
    assert result.returncode == 0
    assert result.stdout == result.stderr == b""
    assert received == [b"save\n"]


def test_helper_subprocess_rejects_directory_override_without_test_mode(tmp_path):
    path = _fifo(_helper_module(), tmp_path)
    env = {**os.environ, "GAME_CONSOLE_COMMAND_DIR": str(tmp_path)}

    result = subprocess.run(
        [str(HELPER_PATH), "terraria-vanilla"],
        env=env,
        input=b"save",
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == result.stderr == b""


def test_installer_deploys_console_command_root_helper():
    files = Installer(Path("/tmp/game-control-test-root"), token_source=Path("/tmp/token")).expected_files()
    destination = Path("/tmp/game-control-test-root/usr/local/libexec/game-console-command")
    assert destination in files
    source, mode = files[destination]
    assert source == HELPER_PATH
    assert mode == 0o755


def test_helper_is_executable_and_not_group_or_world_writable():
    info = HELPER_PATH.stat()
    assert stat.S_IMODE(info.st_mode) == 0o755


def _terraria_profile(
    profile_id: ProfileId = ProfileId.TERRARIA_VANILLA,
    adapter: AdapterKind = AdapterKind.SYSTEMD,
):
    from tests.test_command_backend import _profile

    return _profile(
        adapter,
        profile_id=profile_id,
        operations=frozenset({OperationName.START, OperationName.COMMAND}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["save", "say hello", "time noon"])
async def test_systemd_send_command_uses_fixed_helper_argv_and_stdin(monkeypatch, command):
    adapter = SystemdAdapter()
    calls = []

    async def run(argv, *, timeout, input_data=None):
        calls.append((argv, timeout, input_data))
        return 0, b"", b""

    monkeypatch.setattr(adapter, "_run", run)
    await adapter.send_command(_terraria_profile(), command)

    assert calls == [
        (
            ("/usr/local/libexec/game-console-command", "terraria-vanilla"),
            10.0,
            command.encode(),
        )
    ]


@pytest.mark.asyncio
async def test_systemd_send_command_accepts_pz_rising_allowlist(monkeypatch):
    adapter = SystemdAdapter()
    calls = []

    async def run(argv, *, timeout, input_data=None):
        calls.append((argv, input_data))
        return 0, b"", b""

    monkeypatch.setattr(adapter, "_run", run)
    await adapter.send_command(_terraria_profile(ProfileId.PZ_RISING), "players")
    assert calls == [
        (("/usr/local/libexec/game-console-command", "pz-rising"), b"players")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_id", "command"),
    [
        (ProfileId.MINECRAFT, "save"),
        (ProfileId.TERRARIA_TMOD_145_CANDIDATE, "save\n"),
        (ProfileId.TERRARIA_TMOD, "save\r"),
        (ProfileId.TERRARIA_VANILLA, ""),
        (ProfileId.TERRARIA_VANILLA, "x" * 513),
    ],
)
async def test_systemd_rejects_before_spawn(monkeypatch, profile_id, command):
    adapter = SystemdAdapter()

    async def fail_spawn(*_args, **_kwargs):
        raise AssertionError("rejected command reached process spawn")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fail_spawn)
    with pytest.raises(AdapterError):
        await adapter.send_command(_terraria_profile(profile_id), command)


@pytest.mark.asyncio
async def test_systemd_helper_failure_is_sanitized(monkeypatch):
    adapter = SystemdAdapter()

    async def failed_run(_argv, *, timeout, input_data=None):
        return 2, b"", b"save secret-controller-text"

    monkeypatch.setattr(adapter, "_run", failed_run)
    with pytest.raises(AdapterError) as exc_info:
        await adapter.send_command(_terraria_profile(), "save")
    assert exc_info.value.retryable is True
    assert "save secret-controller-text" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_controller_maps_fixed_helper_nonzero_to_upstream_failure(monkeypatch):
    profile = _terraria_profile(ProfileId.TERRARIA_TMOD_145_CANDIDATE)
    adapter = SystemdAdapter()

    async def failed_run(_argv, *, timeout, input_data=None):
        return 2, b"", b"helper secret-controller-text"

    monkeypatch.setattr(adapter, "_run", failed_run)
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
            action=Command(kind="command", profile_id=profile.id, command="save"),
        )
    )

    assert response.error.code.value == "upstream_unavailable"
    assert response.error.message == "command transport failed"
    assert response.error.retryable is True
    assert "helper secret-controller-text" not in response.model_dump_json()
    job = controller._db().execute(
        "SELECT state, detail FROM jobs"
    ).fetchone()
    assert job == ("failed", "command transport failed")
    audit = controller._db().execute(
        "SELECT result, error_code, detail FROM audit WHERE result='failed'"
    ).fetchone()
    assert audit == ("failed", "upstream_unavailable", "command transport failed")


@pytest.mark.asyncio
async def test_controller_success_audit_contains_no_plaintext_command(monkeypatch):
    profile = _terraria_profile(ProfileId.TERRARIA_TMOD_145_CANDIDATE)
    adapter = SystemdAdapter()

    async def successful_run(_argv, *, timeout, input_data=None):
        return 0, b"", b""

    monkeypatch.setattr(adapter, "_run", successful_run)
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

    assert "save" not in response.model_dump_json()
    canonical = controller._db().execute(
        "SELECT canonical_request FROM rpc_idempotency"
    ).fetchone()[0]
    detail = controller._db().execute("SELECT detail FROM audit").fetchone()[0]
    assert "save" not in canonical
    assert "save" not in detail


@pytest.mark.asyncio
async def test_controller_maps_retryable_command_transport_failure_without_plaintext(monkeypatch):
    profile = _terraria_profile(ProfileId.TERRARIA_TMOD_145_CANDIDATE)
    adapter = SystemdAdapter()

    async def failed_run(_argv, *, timeout, input_data=None):
        raise AdapterError("transport secret", retryable=True)

    monkeypatch.setattr(adapter, "_run", failed_run)
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
            action=Command(kind="command", profile_id=profile.id, command="save"),
        )
    )

    assert response.error.code.value == "upstream_unavailable"
    assert response.error.message == "command transport failed"
    assert response.error.retryable is True
    assert "save" not in response.model_dump_json()
    audit = controller._db().execute(
        "SELECT result, error_code, detail FROM audit WHERE result='failed'"
    ).fetchone()
    assert audit == ("failed", "upstream_unavailable", "command transport failed")
    assert "save" not in str(audit)


@pytest.mark.asyncio
async def test_crafty_command_rejects_transport_failure_without_leaking_details():
    from game_control.adapters.crafty import CraftyAdapter

    class Client:
        async def request(self, *_args, **_kwargs):
            raise OSError("Crafty secret-controller-text")

    adapter = CraftyAdapter("https://crafty.invalid", "token", client=Client())
    with pytest.raises(AdapterError, match="failed") as exc_info:
        await adapter.send_command(_terraria_profile(adapter=AdapterKind.CRAFTY), "save")
    assert "secret-controller-text" not in str(exc_info.value)
