#!/usr/bin/env python3
"""Install the fixed game-control package without touching external services."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROFILE_FILES = tuple(sorted((PACKAGE_ROOT / "config/profiles").glob("*.toml")))
RUNNER_FILES = tuple(sorted((PACKAGE_ROOT / "config/runner").glob("*.json")))
UNIT_FILES = tuple(sorted((PACKAGE_ROOT / "ops/systemd").glob("*.service")))
UNIT_DROPIN_FILES = tuple(sorted((PACKAGE_ROOT / "ops/systemd").glob("*.service.d/*.conf")))
TMPFILES = PACKAGE_ROOT / "ops/tmpfiles/game-control.conf"
SLOT_RUNNER = PACKAGE_ROOT / "ops/bin/game-slot-run"
CONSOLE_STOPPER = PACKAGE_ROOT / "ops/bin/game-console-stop"
CONSOLE_COMMAND = PACKAGE_ROOT / "ops/bin/game-console-command"
ROOT_CONFIG = PACKAGE_ROOT / "config/game-control.toml"


class Installer:
    def __init__(self, root: Path, *, token_source: Path, skip_systemd_verify: bool = False):
        self.root = root
        self.token_source = token_source
        self.skip_systemd_verify = skip_systemd_verify

    def target(self, path: str | Path) -> Path:
        value = Path(path)
        return self.root / value.relative_to("/") if value.is_absolute() else self.root / value

    def expected_files(self) -> dict[Path, tuple[Path, int]]:
        files: dict[Path, tuple[Path, int]] = {}
        for source in PROFILE_FILES:
            files[self.target(f"/etc/game-control/profiles.d/{source.name}")] = (source, 0o644)
        for source in RUNNER_FILES:
            files[self.target(f"/etc/game-control/runner.d/{source.name}")] = (source, 0o644)
        for source in UNIT_FILES:
            files[self.target(f"/etc/systemd/system/{source.name}")] = (source, 0o644)
        for source in UNIT_DROPIN_FILES:
            relative = source.relative_to(PACKAGE_ROOT / "ops/systemd")
            files[self.target(f"/etc/systemd/system/{relative}")] = (source, 0o644)
        files[self.target("/usr/lib/tmpfiles.d/game-control.conf")] = (TMPFILES, 0o644)
        files[self.target("/usr/local/libexec/game-slot-run")] = (SLOT_RUNNER, 0o755)
        files[self.target("/usr/local/libexec/game-console-stop")] = (CONSOLE_STOPPER, 0o755)
        files[self.target("/usr/local/libexec/game-console-command")] = (CONSOLE_COMMAND, 0o755)
        files[self.target("/etc/game-control/game-control.toml")] = (ROOT_CONFIG, 0o600)
        return files

    def directories(self) -> tuple[tuple[Path, int, str, str], ...]:
        return (
            (self.target("/etc/game-control"), 0o755, "root", "root"),
            (self.target("/etc/game-control/profiles.d"), 0o755, "root", "root"),
            (self.target("/etc/game-control/runner.d"), 0o755, "root", "root"),
            (self.target("/etc/game-control/secrets.d"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control-web"), 0o700, "gamecontrol", "gamecontrol"),
            (self.target("/run/game-control"), 0o755, "root", "root"),
            (self.target("/run/game-slot"), 0o770, "root", "gameslot"),
            (self.target("/opt/game-servers"), 0o755, "root", "root"),
            (self.target("/srv/game-servers"), 0o755, "root", "root"),
            (self.target("/opt/game-servers/terraria-vanilla"), 0o755, "root", "root"),
            (self.target("/opt/game-servers/terraria-tmod"), 0o755, "root", "root"),
            (self.target("/srv/game-servers/terraria-vanilla"), 0o755, "root", "root"),
            (self.target("/srv/game-servers/terraria-tmod"), 0o755, "root", "root"),
            *(
                (self.target(f"/srv/game-servers/{profile}/{subdir}"), 0o750, user, user)
                for profile, user in (
                    ("terraria-vanilla", "terraria-vanilla"),
                    ("terraria-tmod", "tmodloader"),
                )
                for subdir in ("config", "worlds", "mods", "logs", "backups")
            ),
            (self.target("/srv/game-servers/terraria-vanilla/.local"), 0o700, "terraria-vanilla", "terraria-vanilla"),
            (self.target("/srv/game-servers/terraria-vanilla/.local/share"), 0o700, "terraria-vanilla", "terraria-vanilla"),
            (self.target("/srv/game-servers/terraria-vanilla/.local/share/Terraria"), 0o700, "terraria-vanilla", "terraria-vanilla"),
            (self.target("/srv/game-servers/terraria-tmod/.local"), 0o700, "tmodloader", "tmodloader"),
            (self.target("/srv/game-servers/terraria-tmod/.local/share"), 0o700, "tmodloader", "tmodloader"),
            (self.target("/srv/game-servers/terraria-tmod/.local/share/Terraria"), 0o700, "tmodloader", "tmodloader"),
            (self.target("/var/backups/game-servers"), 0o700, "root", "root"),
        )

    def drift(self) -> list[str]:
        problems: list[str] = []
        for path, mode, _user, _group in self.directories():
            if not path.is_dir():
                problems.append(f"missing directory {path}")
            elif stat.S_IMODE(path.stat().st_mode) != mode:
                problems.append(f"mode drift {path}")
        for destination, (_source, mode) in self.expected_files().items():
            if not destination.is_file():
                problems.append(f"missing file {destination}")
            elif stat.S_IMODE(destination.stat().st_mode) != mode:
                problems.append(f"mode drift {destination}")
        token = self.target("/etc/game-control/secrets.d/crafty-token")
        if not token.is_file():
            problems.append(f"missing file {token}")
        elif stat.S_IMODE(token.stat().st_mode) != 0o600:
            problems.append(f"mode drift {token}")
        elif not self.token_source.is_file():
            problems.append("Crafty token source is unavailable")
        else:
            source_hash = hashlib.sha256(self.token_source.read_bytes()).digest()
            target_hash = hashlib.sha256(token.read_bytes()).digest()
            if source_hash != target_hash:
                problems.append(f"content drift {token}")
        return problems

    @staticmethod
    def _lookup(name: str, group: bool = False) -> int:
        import grp
        import pwd

        return (grp.getgrnam(name).gr_gid if group else pwd.getpwnam(name).pw_uid)

    def _chown(self, path: Path, user: str, group: str) -> None:
        if self.root != Path("/"):
            return
        try:
            os.chown(path, self._lookup(user), self._lookup(group, group=True))
        except KeyError:
            return

    def _mkdir(self, path: Path, mode: int, user: str, group: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        self._chown(path, user, group)

    @staticmethod
    def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                with source.open("rb") as source_stream:
                    shutil.copyfileobj(source_stream, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _accounts(self) -> None:
        if self.root != Path("/"):
            return
        subprocess.run(["/usr/sbin/groupadd", "--system", "gameslot"], check=False)
        for group in ("gamecontrol", "terraria-vanilla", "tmodloader"):
            subprocess.run(["/usr/sbin/groupadd", "--system", group], check=False)
        for user, group in (
            ("gameslot", "gameslot"),
            ("gamecontrol", "gamecontrol"),
            ("terraria-vanilla", "terraria-vanilla"),
            ("tmodloader", "tmodloader"),
        ):
            subprocess.run(
                ["/usr/sbin/useradd", "--system", "--no-create-home", "--gid", group, "--shell", "/usr/sbin/nologin", user],
                check=False,
            )
        for user in ("crafty", "pzuser", "terraria-vanilla", "tmodloader"):
            subprocess.run(
                ["/usr/sbin/usermod", "--append", "--groups", "gameslot", user],
                check=False,
            )

    def apply(self) -> None:
        self._accounts()
        for path, mode, user, group in self.directories():
            self._mkdir(path, mode, user, group)
        for profile, user in (
            ("terraria-vanilla", "terraria-vanilla"),
            ("terraria-tmod", "tmodloader"),
        ):
            log = self.target(f"/srv/game-servers/{profile}/logs/server.log")
            log.touch(exist_ok=True)
            os.chmod(log, 0o600)
            self._chown(log, user, user)
        for destination, (source, mode) in self.expected_files().items():
            self._atomic_copy(source, destination, mode)
            self._chown(destination, "root", "root")
        token_destination = self.target("/etc/game-control/secrets.d/crafty-token")
        self._atomic_copy(self.token_source, token_destination, 0o600)
        self._chown(token_destination, "root", "root")
        if self.root == Path("/"):
            venv = self.target("/opt/game-control/.venv")
            if not venv.exists():
                subprocess.run(["/usr/bin/python3", "-m", "venv", str(venv)], check=True)
            subprocess.run(
                [
                    str(venv / "bin/python"),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-input",
                    str(self.target("/opt/game-control")),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        if not self.skip_systemd_verify:
            units = [str(path) for path in self.expected_files() if path.name.endswith(".service")]
            subprocess.run(["/usr/bin/systemd-analyze", "verify", *units], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--apply", action="store_true", help="apply the package atomically")
    parser.add_argument("--root", type=Path, default=None, help="install below an alternate root (defaults to /)")
    parser.add_argument("--token-source", type=Path, default=None, help="path to the Crafty API token source")
    parser.add_argument("--skip-systemd-verify", action="store_true", help="skip systemd-analyze verification")
    args = parser.parse_args(argv)
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")
    root = args.root or Path(os.environ.get("GAME_CONTROL_INSTALL_ROOT", "/"))
    token_value = args.token_source or os.environ.get("GAME_CONTROL_TOKEN_SOURCE")
    if token_value is None:
        print("Crafty token source must be supplied with --token-source or GAME_CONTROL_TOKEN_SOURCE", file=sys.stderr)
        return 2
    token = Path(token_value)
    installer = Installer(root, token_source=token, skip_systemd_verify=args.skip_systemd_verify)
    if args.check:
        drift = installer.drift()
        if drift:
            for item in drift:
                print(item)
            return 1
        return 0
    if not installer.token_source.is_file():
        print("Crafty token source is unavailable", file=sys.stderr)
        return 1
    installer.apply()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
