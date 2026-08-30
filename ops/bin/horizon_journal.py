#!/usr/bin/env python3
"""Closed-path evidence and finalization helpers for the horizon journal namespace."""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SUNLIT_PROFILE = "minecraft-sunlit-cobblemon"
SUNLIT_UNIT = "minecraft-sunlit-cobblemon.service"
NAMESPACE = "horizon"
JOURNALCTL = "/usr/bin/journalctl"
EVIDENCE_DIR = Path("/var/lib/game-control/horizon-journal")
EVIDENCE_FILE = EVIDENCE_DIR / "boot-evidence.jsonl"
FINAL_DROPIN = Path("/etc/systemd/journald@horizon.conf.d/30-runtime-rate-limit.conf")
UUID32 = re.compile(r"^[0-9a-fA-F]{32}$")
BOOT_MESSAGE = re.compile(r"(?:\bDone \(|HORIZON_BOOT_COMPLETE=1)")
SUPPRESSION_MESSAGE = re.compile(r"suppressed\s+\d+\s+messages", re.IGNORECASE)


def validate_invocation_id(value: str) -> str:
    if not UUID32.fullmatch(value):
        raise ValueError("invalid invocation id")
    return value.lower()


def _secure_dir(path: Path) -> None:
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("insecure evidence directory")
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != 0 or ((mode & 0o022) and not (mode & 0o1000)):
            raise RuntimeError("evidence directory is not root-only")


def _secure_existing_file(path: Path) -> None:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("insecure evidence or override file")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("insecure evidence or override file")


def _load_evidence(path: Path = EVIDENCE_FILE) -> list[dict[str, Any]]:
    _secure_dir(path.parent)
    if not path.exists():
        return []
    _secure_existing_file(path)
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise RuntimeError("malformed evidence") from exc
            if not isinstance(record, dict):
                raise RuntimeError("malformed evidence")
            if not isinstance(record.get("invocation_id"), str):
                raise RuntimeError("malformed evidence")
            validate_invocation_id(record["invocation_id"])
            if record.get("unit") != SUNLIT_UNIT or record.get("profile") != SUNLIT_PROFILE:
                raise RuntimeError("evidence target is not fixed Sunlit")
            if not isinstance(record.get("complete_boot"), bool):
                raise RuntimeError("malformed evidence")
            if not isinstance(record.get("peak_30s_lines"), int) or record["peak_30s_lines"] < 0:
                raise RuntimeError("malformed evidence")
            records.append(record)
    return records


def _journal_entries(invocation_id: str) -> list[dict[str, Any]]:
    invocation_id = validate_invocation_id(invocation_id)
    command = [
        JOURNALCTL,
        f"--namespace={NAMESPACE}",
        "--output=json",
        "--no-pager",
        f"_SYSTEMD_UNIT={SUNLIT_UNIT}",
        f"_SYSTEMD_INVOCATION_ID={invocation_id}",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("journal evidence unavailable") from exc
    entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("malformed journal evidence") from exc
        if not isinstance(entry, dict):
            raise RuntimeError("malformed journal evidence")
        if entry.get("_SYSTEMD_UNIT") != SUNLIT_UNIT or entry.get("_SYSTEMD_INVOCATION_ID", "").lower() != invocation_id:
            raise RuntimeError("journal evidence target mismatch")
        try:
            int(entry["__REALTIME_TIMESTAMP"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("journal evidence timestamp missing") from exc
        entries.append(entry)
    if not entries:
        raise RuntimeError("no journal evidence")
    return entries


def _namespace_window_entries(start_us: int, end_us: int) -> list[dict[str, Any]]:
    if start_us < 0 or end_us < start_us:
        raise RuntimeError("invalid journal evidence window")
    command = [
        JOURNALCTL,
        f"--namespace={NAMESPACE}",
        "--output=json",
        "--no-pager",
        f"--since=@{start_us / 1_000_000:.6f}",
        f"--until=@{end_us / 1_000_000:.6f}",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("journal suppression evidence unavailable") from exc
    entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("malformed journal suppression evidence") from exc
        if not isinstance(entry, dict):
            raise RuntimeError("malformed journal suppression evidence")
        entries.append(entry)
    return entries


def peak_rolling_30s(entries: list[dict[str, Any]]) -> int:
    timestamps = sorted(int(entry["__REALTIME_TIMESTAMP"]) for entry in entries)
    window: collections.deque[int] = collections.deque()
    peak = 0
    for timestamp in timestamps:
        window.append(timestamp)
        while window and timestamp - window[0] > 30_000_000:
            window.popleft()
        peak = max(peak, len(window))
    return peak


def _is_complete_boot(entries: list[dict[str, Any]]) -> bool:
    return any(
        entry.get("HORIZON_BOOT_COMPLETE") == "1"
        or (isinstance(entry.get("MESSAGE"), str) and BOOT_MESSAGE.search(entry["MESSAGE"]))
        for entry in entries
    )


def _append_record(record: dict[str, Any], path: Path = EVIDENCE_FILE) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("not root")
    _secure_dir(path.parent)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("insecure evidence file")
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def capture(invocation_id: str, *, evidence_path: Path = EVIDENCE_FILE) -> dict[str, Any]:
    invocation_id = validate_invocation_id(invocation_id)
    entries = _journal_entries(invocation_id)
    record = {
        "profile": SUNLIT_PROFILE,
        "unit": SUNLIT_UNIT,
        "invocation_id": invocation_id,
        "complete_boot": _is_complete_boot(entries),
        "journal_lines": len(entries),
        "peak_30s_lines": peak_rolling_30s(entries),
    }
    _append_record(record, evidence_path)
    return record


def _secure_override_parent(path: Path) -> None:
    _secure_dir(path.parent)
    if path.exists() or path.is_symlink():
        _secure_existing_file(path)


def finalize(*, evidence_path: Path = EVIDENCE_FILE, override_path: Path = FINAL_DROPIN) -> int:
    records = _load_evidence(evidence_path)
    complete = [record for record in records if record["complete_boot"]]
    invocation_ids = {record["invocation_id"] for record in complete}
    if len(invocation_ids) < 2:
        raise RuntimeError("two distinct complete boots are required")
    peak = max(record["peak_30s_lines"] for record in complete)
    burst = max(1, (peak * 3 + 1) // 2)
    _secure_override_parent(override_path)
    content = f"[Journal]\nRateLimitIntervalSec=30s\nRateLimitBurst={burst}\n"
    fd, temporary = tempfile.mkstemp(prefix=".horizon-rate-limit.", dir=str(override_path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, override_path)
        os.chmod(override_path, 0o600)
    except (OSError, RuntimeError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return burst


def verify_zero_suppression(invocation_id: str) -> bool:
    entries = _journal_entries(invocation_id)
    _secure_existing_file(FINAL_DROPIN)
    override = FINAL_DROPIN.read_text(encoding="utf-8")
    if "RateLimitIntervalSec=30s" not in override:
        raise RuntimeError("runtime journal rate limit is not finalized")
    burst = re.search(r"(?m)^RateLimitBurst=(\d+)$", override)
    if burst is None or int(burst.group(1)) < 1:
        raise RuntimeError("runtime journal rate limit is not finalized")
    timestamps = [int(entry["__REALTIME_TIMESTAMP"]) for entry in entries]
    window = _namespace_window_entries(min(timestamps), max(timestamps))
    suppressed = [
        entry
        for entry in window
        if isinstance(entry.get("MESSAGE"), str)
        and SUPPRESSION_MESSAGE.search(entry["MESSAGE"])
    ]
    if suppressed:
        raise RuntimeError("journal suppression was observed")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="horizon-journal-evidence")
    sub = parser.add_subparsers(dest="operation", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--profile", choices=[SUNLIT_PROFILE], default=SUNLIT_PROFILE)
    capture_parser.add_argument("invocation_id")
    zero_parser = sub.add_parser("zero-suppression")
    zero_parser.add_argument("--profile", choices=[SUNLIT_PROFILE], default=SUNLIT_PROFILE)
    zero_parser.add_argument("invocation_id")
    sub.add_parser("finalize")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.operation == "capture":
            print(json.dumps(capture(args.invocation_id), sort_keys=True))
        elif args.operation == "zero-suppression":
            verify_zero_suppression(args.invocation_id)
            print("zero suppression proved")
        else:
            print(f"RateLimitBurst={finalize()}")
        return 0
    except (OSError, RuntimeError, ValueError):
        sys.stderr.write("horizon journal evidence rejected\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
