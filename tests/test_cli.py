from __future__ import annotations

import argparse

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

    monkeypatch.setattr(cli, "_pending", handler)
    assert cli.main(["backup", "reconcile", "--apply"]) == 7
    assert calls == [("backup", True)]
