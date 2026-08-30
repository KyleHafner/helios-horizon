"""Closed, typed operator CLI for Horizon.

The command tree is intentionally declared in source.  It is not a generic
dispatcher: no command, handler, executable, unit, URL, or filesystem policy
is selected by an input string.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any


_UNSAFE_TEXT = re.compile(r"[;&|$`()<>\x00-\x1f]")
def _pending(_args: argparse.Namespace) -> int:
    """W5.1 placeholder replaced by the typed handler table in W5.2."""

    raise RuntimeError("Horizon CLI handlers are not installed")


def _reject_duplicate_options(argv: Sequence[str]) -> None:
    seen: set[str] = set()
    value_options = {"--root", "--relay-state"}
    index = 0
    while index < len(argv):
        value = argv[index]
        if value.startswith("--"):
            option = value.split("=", 1)[0]
            if option in {"--apply", "--static", "--check", "--root", "--relay-state"}:
                if option in seen:
                    raise ValueError(f"duplicate option: {option}")
                seen.add(option)
                if option in value_options and "=" not in value:
                    index += 1
                    if index >= len(argv):
                        break
        index += 1


def _reject_unsafe_values(argv: Sequence[str]) -> None:
    for value in argv:
        if _UNSAFE_TEXT.search(value):
            raise ValueError("unsafe command argument")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="horizon")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    reconcile = backup_commands.add_parser("reconcile")
    reconcile.add_argument("--apply", action="store_true")
    reconcile.set_defaults(handler=_pending)

    capability = commands.add_parser("capability")
    capability_commands = capability.add_subparsers(dest="capability_command", required=True)
    issue = capability_commands.add_parser("issue")
    issue.set_defaults(handler=_pending)

    deployment = commands.add_parser("deployment")
    deployment_commands = deployment.add_subparsers(dest="deployment_command", required=True)
    verify = deployment_commands.add_parser("verify")
    verify.add_argument("--static", action="store_true")
    verify.add_argument("--root", type=Path)
    verify.add_argument("--relay-state", choices=("private", "production"))
    verify.set_defaults(handler=_pending)

    journal = commands.add_parser("journal")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)
    capture = journal_commands.add_parser("capture")
    capture.add_argument("invocation_id", type=_invocation_id)
    capture.set_defaults(handler=_pending)
    suppression = journal_commands.add_parser("zero-suppression")
    suppression.add_argument("invocation_id", type=_invocation_id)
    suppression.set_defaults(handler=_pending)
    finalize = journal_commands.add_parser("finalize")
    finalize.set_defaults(handler=_pending)

    session = commands.add_parser("session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    revoke = session_commands.add_parser("revoke-all")
    revoke.set_defaults(handler=_pending)

    tuning = commands.add_parser("tuning")
    tuning_commands = tuning.add_subparsers(dest="tuning_command", required=True)
    jvm = tuning_commands.add_parser("jvm-args")
    jvm.add_argument("action", choices=("verify", "activate", "rollback"))
    jvm.set_defaults(handler=_pending)

    sunlit = commands.add_parser("sunlit")
    sunlit_commands = sunlit.add_subparsers(dest="sunlit_command", required=True)
    update = sunlit_commands.add_parser("update")
    update.add_argument("--check", action="store_true")
    update.set_defaults(handler=_pending)

    return parser


def _invocation_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise argparse.ArgumentTypeError("invocation ID must be exactly 32 hexadecimal characters")
    return value.lower()


def build_parser() -> argparse.ArgumentParser:
    return _parser()


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv) if argv is not None else None
    try:
        if values is not None:
            _reject_duplicate_options(values)
            _reject_unsafe_values(values)
        args = _parser().parse_args(values)
    except ValueError as exc:
        raise SystemExit(f"horizon: error: {exc}") from exc
    handler: Any = vars(args).get("handler")
    if handler is None:
        raise SystemExit("horizon: error: command is not registered")
    return int(handler(args))


__all__ = ["build_parser", "main"]
