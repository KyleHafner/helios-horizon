from __future__ import annotations

import argparse
import sys

import pytest

from game_control import cli


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["backup", "reconcile"], "backup"),
        (["backup", "reconcile", "--apply"], "backup"),
        (["capability", "issue"], "capability"),
        (["deployment", "verify"], "deployment"),
        (["deployment", "verify", "--static", "--root", "/tmp/root", "--relay-state", "private"], "deployment"),
        (["journal", "capture", "0123456789abcdef0123456789abcdef"], "journal"),
        (["journal", "zero-suppression", "0123456789abcdef0123456789abcdef"], "journal"),
        (["journal", "finalize"], "journal"),
        (["session", "revoke-all"], "session"),
        (["tuning", "jvm-args", "verify"], "tuning"),
        (["tuning", "jvm-args", "activate"], "tuning"),
        (["tuning", "jvm-args", "rollback"], "tuning"),
        (["sunlit", "update"], "sunlit"),
        (["sunlit", "update", "--check"], "sunlit"),
    ],
)
def test_closed_commands_parse_without_dispatch(argv: list[str], command: str) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.command == command
    assert callable(vars(args)["handler"])


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "anything"],
        ["systemd", "restart"],
        ["exec", "/bin/sh"],
        ["backup", "reconcile", "--apply", "--apply"],
        ["backup", "reconcile", "--exec=/bin/sh"],
        ["deployment", "verify", "--unit", "x"],
        ["deployment", "verify", "--root", "/tmp/a;touch"],
        ["sunlit", "manifest"],
        ["state", "migrate"],
        ["journal", "capture", "not-an-id"],
        ["journal", "capture", "0123456789abcdef0123456789abcde0", "extra"],
        ["tuning", "jvm-args", "activate", "rollback"],
    ],
)
def test_closed_commands_reject_before_handler(argv: list[str]) -> None:
    with pytest.raises((SystemExit, ValueError)):
        cli.main(argv)


def test_main_dispatches_direct_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    def handler(args: argparse.Namespace) -> int:
        calls.append((args.command, args.apply))
        return 7

    monkeypatch.setattr(cli, "_backup", handler)
    assert cli.main(["backup", "reconcile", "--apply"]) == 7
    assert calls == [("backup", True)]


_HANDLERS = ("_backup", "_capability", "_deployment", "_journal", "_session", "_jvm", "_sunlit")


@pytest.mark.parametrize(
    "argv",
    [
        ["backup", "reconcile", "--apply", "--apply"],
        ["deployment", "verify", "--static", "--static"],
        ["sunlit", "update", "--check", "--check"],
        ["backup", "reconcile", "extra"],
        ["journal", "finalize", "extra"],
        ["journal", "capture", "0123456789abcdef0123456789abcdef", "extra"],
        ["tuning", "jvm-args", "activate", "rollback"],
        ["capability", "issue", "$HOME"],
        ["deployment", "verify", "--root", "/tmp/a;touch"],
    ],
)
def test_console_rejects_adversarial_input_before_any_handler(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    calls: list[str] = []

    def handler(_args: argparse.Namespace) -> int:
        calls.append("handler")
        return 0

    for name in _HANDLERS:
        monkeypatch.setattr(cli, name, handler)
    monkeypatch.setattr(sys, "argv", ["horizon", *argv])

    with pytest.raises(SystemExit):
        cli.main()

    assert calls == []


@pytest.mark.parametrize(
    ("argv", "expected_command"),
    [
        (["backup", "reconcile"], "backup"),
        (["capability", "issue"], "capability"),
        (["deployment", "verify", "--static"], "deployment"),
        (["journal", "capture", "0123456789abcdef0123456789abcdef"], "journal"),
        (["journal", "zero-suppression", "0123456789abcdef0123456789abcdef"], "journal"),
        (["journal", "finalize"], "journal"),
        (["session", "revoke-all"], "session"),
        (["tuning", "jvm-args", "verify"], "tuning"),
        (["sunlit", "update", "--check"], "sunlit"),
    ],
)
def test_console_dispatches_valid_commands_exactly_once(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_command: str
) -> None:
    calls: list[str] = []

    def handler(args: argparse.Namespace) -> int:
        calls.append(args.command)
        return 23

    for name in _HANDLERS:
        monkeypatch.setattr(cli, name, handler)
    monkeypatch.setattr(sys, "argv", ["horizon", *argv])

    assert cli.main() == 23
    assert calls == [expected_command]
