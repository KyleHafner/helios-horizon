#!/usr/bin/env python3
"""Fail-closed scanner for the public repository boundary."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


MAX_TRACKED_FILES = 50_000
MAX_TRACKED_LIST_BYTES = 4 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 8 * 1024 * 1024

BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".avif",
        ".bmp",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".otf",
        ".pdf",
        ".png",
        ".tar",
        ".ttf",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)

FORBIDDEN_DOMAIN = re.compile(
    r"(?:[A-Za-z0-9-]+[.])*" + "heliosorbit" + r"[.]" + "space",
    re.IGNORECASE,
)
IPV4_CANDIDATE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}[.]){3}[0-9]{1,3}(?![0-9])")
PRIVATE_DNS = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
    r"(?:[.][A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*"
    r"[.](?:local|lan|internal|home)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.:/-])/(?:[A-Za-z0-9_.~!$&'()*+,;=:@%+-]+/?)+")
FILE_URI = re.compile(r"(?<![A-Za-z0-9_.-])file:", re.IGNORECASE)

REFUSED_NETWORKS = tuple(
    ipaddress.ip_network(".".join(parts) + suffix)
    for parts, suffix in (
        (("10", "0", "0", "0"), "/8"),
        (("172", "16", "0", "0"), "/12"),
        (("192", "168", "0", "0"), "/16"),
        (("100", "64", "0", "0"), "/10"),
    )
)

# These tokens are programming-language identifiers, not DNS names. Keep this
# list exact and small; additions require a test and a reviewable reason.
NON_DNS_TOKENS = frozenset(
    {
        "BackupDestination" + "." + "LOCAL",  # Python enum member.
        "user" + "." + "home",  # Java system property.
    }
)


class ScanError(RuntimeError):
    """The repository could not be scanned completely and safely."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    category: str

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.category}"


def _validate_policy() -> None:
    if not REFUSED_NETWORKS or any(
        network.version != 4 or not isinstance(network.prefixlen, int)
        for network in REFUSED_NETWORKS
    ):
        raise ScanError("invalid-network-policy")
    if any(not token or "\n" in token or "\x00" in token for token in NON_DNS_TOKENS):
        raise ScanError("invalid-dns-allowlist")
    if any(not suffix.startswith(".") for suffix in BINARY_SUFFIXES):
        raise ScanError("invalid-binary-policy")


def _tracked_paths(root: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError("tracked-file-enumeration-failed") from exc
    if len(result.stdout) > MAX_TRACKED_LIST_BYTES:
        raise ScanError("tracked-file-list-too-large")
    raw_paths = result.stdout.split(b"\x00")
    if raw_paths and raw_paths[-1] == b"":
        raw_paths.pop()
    if len(raw_paths) > MAX_TRACKED_FILES:
        raise ScanError("too-many-tracked-files")
    paths: list[str] = []
    for raw_path in raw_paths:
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ScanError("tracked-path-is-not-utf8") from exc
        candidate = Path(path)
        if (
            not path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or ":" in path
            or any(ord(character) < 32 for character in path)
        ):
            raise ScanError("unsafe-tracked-path")
        paths.append(candidate.as_posix())
    if len(set(paths)) != len(paths):
        raise ScanError("duplicate-tracked-path")
    return tuple(sorted(paths))


def _is_binary_asset(relative: str) -> bool:
    return Path(relative).suffix.lower() in BINARY_SUFFIXES


def _is_example_surface(relative: str) -> bool:
    return relative.startswith("config/examples/") or relative.startswith("deploy/example/")


def _allowed_example_path(value: str) -> bool:
    if "\\" in value or "//" in value:
        return False
    candidate = PurePosixPath(value)
    if not candidate.is_absolute() or any(part in {".", ".."} for part in candidate.parts):
        return False
    if candidate.as_posix() != value:
        return False
    exact_prefixes = (
        "/etc/horizon-example",
        "/var/lib/horizon-example",
        "/run/horizon-example",
    )
    if any(value == prefix or value.startswith(prefix + "/") for prefix in exact_prefixes):
        return True
    family_prefixes = (
        "/srv/example-",
        "/opt/example-",
        "/var/backups/example-",
        "/usr/local/libexec/horizon-example-",
    )
    return any(value.startswith(prefix) and len(value) > len(prefix) for prefix in family_prefixes)


def _line_findings(relative: str, line_number: int, line: str) -> set[Finding]:
    findings: set[Finding] = set()
    if FORBIDDEN_DOMAIN.search(line):
        findings.add(Finding(relative, line_number, "private-domain"))
    for match in IPV4_CANDIDATE.finditer(line):
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        if any(address in network for network in REFUSED_NETWORKS):
            findings.add(Finding(relative, line_number, "private-address"))
    for match in PRIVATE_DNS.finditer(line):
        if match.group(0) not in NON_DNS_TOKENS:
            findings.add(Finding(relative, line_number, "private-dns"))
    if _is_example_surface(relative):
        if FILE_URI.search(line):
            findings.add(Finding(relative, line_number, "example-path-namespace"))
        for match in ABSOLUTE_PATH.finditer(line):
            if not _allowed_example_path(match.group(0).rstrip("/")):
                findings.add(Finding(relative, line_number, "example-path-namespace"))
    return findings


def scan_repository(root: Path) -> tuple[Finding, ...]:
    _validate_policy()
    repository = root.resolve(strict=True)
    try:
        top_level = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError("repository-root-verification-failed") from exc
    if Path(top_level).resolve(strict=True) != repository:
        raise ScanError("root-is-not-repository-top-level")

    findings: set[Finding] = set()
    for relative in _tracked_paths(repository):
        if _is_binary_asset(relative):
            continue
        path = repository / relative
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ScanError(f"tracked-file-unreadable:{relative}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ScanError(f"tracked-file-not-regular:{relative}")
        if metadata.st_size > MAX_TEXT_FILE_BYTES:
            raise ScanError(f"tracked-text-too-large:{relative}")
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            raise ScanError(f"tracked-text-unreadable:{relative}") from exc
        for line_number, line in enumerate(text.splitlines(), start=1):
            findings.update(_line_findings(relative, line_number, line))
    return tuple(sorted(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="explicit repository root")
    args = parser.parse_args(argv)
    try:
        findings = scan_repository(args.root)
    except ScanError as exc:
        category = str(exc).split(":", 1)[0] or "scan-error"
        print(f"<repository>:0:{category}", file=sys.stderr)
        return 2
    except OSError:
        print("<repository>:0:repository-unreadable", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding.render())
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
