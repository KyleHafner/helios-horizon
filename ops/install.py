#!/usr/bin/env python3
"""Install the fixed game-control package without touching external services."""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PROFILE_IDS = (
    "minecraft-sunlit-cobblemon",
    "terraria-vanilla",
    "terraria-tmod",
)
PROFILE_FILES = tuple(
    PACKAGE_ROOT / "config/profiles" / f"{profile_id}.toml"
    for profile_id in ACTIVE_PROFILE_IDS
)
RUNNER_FILES = tuple(
    PACKAGE_ROOT / "config/runner" / f"{profile_id}.json"
    for profile_id in ACTIVE_PROFILE_IDS
)
WEB_FILES = tuple(
    PACKAGE_ROOT / "web" / name
    for name in ("app.js", "commands.js", "index.html", "palette.js", "styles.css")
)
UNIT_FILES = tuple(
    PACKAGE_ROOT / "ops/systemd" / name
    for name in (
        "game-control-web.service",
        "game-slotd.service",
        "horizon-bore-liveness.service",
        "horizon-bore-liveness.timer",
        "horizon-sunlit-auto-update.service",
        "horizon-sunlit-auto-update.timer",
        "lazymc-minecraft.service",
        "bore-minecraft-fenced.service",
        "horizon-alert-drill@.service",
        "horizon-alert-notify@.service",
        "horizon-terraria-relay.service",
        "minecraft-sunlit-cobblemon.service",
        "terraria-tmod.service",
        "terraria-vanilla.service",
    )
)
UNIT_DROPIN_FILES = (
    PACKAGE_ROOT / "ops/systemd/game-slotd.service.d/io-metrics.conf",
    PACKAGE_ROOT / "ops/systemd/minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf",
)
LAZYMC_CONFIG = PACKAGE_ROOT / "ops/lazymc/lazymc.toml"
LAZYMC_SERVER_PROPERTIES = PACKAGE_ROOT / "ops/lazymc/server.properties"
SLICE_FILES = tuple(PACKAGE_ROOT / "ops/systemd" / name for name in ("games.slice", "horizon.slice", "maintenance.slice"))
TMPFILES = PACKAGE_ROOT / "ops/tmpfiles/game-control.conf"
ROOT_CONFIG = PACKAGE_ROOT / "config/game-control.toml"
NFTABLES_POLICY = PACKAGE_ROOT / "ops/nftables/horizon.nft"
JOURNAL_BASE = PACKAGE_ROOT / "ops/journald/horizon.conf"
JOURNAL_MEASUREMENT = PACKAGE_ROOT / "ops/journald/horizon-private-measurement.conf"
RUNTIME_SOURCE_FILES = tuple(sorted((PACKAGE_ROOT / "src/game_control").rglob("*.py")))
RUNTIME_VERIFIER = PACKAGE_ROOT / "scripts/verify-deployed.py"
RUNTIME_SUPPORT_FILES = (
    (PACKAGE_ROOT / "pyproject.toml", "pyproject.toml", 0o644),
    (PACKAGE_ROOT / "ops/install.py", "ops/install.py", 0o755),
)
RUNTIME_MANIFEST_PATH = "/opt/game-control/.horizon-runtime-manifest"
RUNTIME_MANIFEST_VERSION = "1"
HELPER_FILES = {
    "game-slot-run": (PACKAGE_ROOT / "ops/bin/game-slot-run", 0o755),
    "game-console-stop": (PACKAGE_ROOT / "ops/bin/game-console-stop", 0o755),
    "game-console-command": (PACKAGE_ROOT / "ops/bin/game-console-command", 0o755),
    "game-sunlit-prepare": (PACKAGE_ROOT / "ops/bin/game-sunlit-prepare", 0o755),
    "game-sunlit-rcon-prepare": (PACKAGE_ROOT / "ops/bin/game-sunlit-rcon-prepare", 0o755),
    "game-sunlit-stop": (PACKAGE_ROOT / "ops/bin/game-sunlit-stop", 0o755),
    "horizon-capability-issue": (PACKAGE_ROOT / "ops/bin/horizon-capability-issue", 0o755),
    "horizon-alert-notify": (PACKAGE_ROOT / "ops/bin/horizon-alert-notify", 0o755),
    "horizon-backup-reconcile": (PACKAGE_ROOT / "ops/bin/horizon-backup-reconcile", 0o755),
    "horizon-sunlit-promote": (PACKAGE_ROOT / "ops/bin/horizon-sunlit-promote", 0o755),
    "horizon-sunlit-manifest": (PACKAGE_ROOT / "ops/bin/horizon-sunlit-manifest", 0o755),
    "horizon-sunlit-stage": (PACKAGE_ROOT / "ops/bin/horizon-sunlit-stage", 0o755),
    "horizon-sunlit-auto-update": (PACKAGE_ROOT / "ops/bin/horizon-sunlit-auto-update", 0o755),
    "horizon-sunlit-update-rpc": (PACKAGE_ROOT / "ops/bin/horizon-sunlit-update-rpc", 0o755),
    "horizon-bore-liveness": (PACKAGE_ROOT / "ops/bin/horizon-bore-liveness", 0o755),
    "horizon-lazymc-wake": (PACKAGE_ROOT / "ops/bin/horizon-lazymc-wake", 0o755),
    "horizon-journal-evidence": (PACKAGE_ROOT / "ops/bin/horizon-journal-evidence", 0o755),
    "horizon-journal-finalize": (PACKAGE_ROOT / "ops/bin/horizon-journal-finalize", 0o755),
    "horizon_journal.py": (PACKAGE_ROOT / "ops/bin/horizon_journal.py", 0o644),
    "horizon-session-revoke-all": (PACKAGE_ROOT / "ops/bin/horizon-session-revoke-all", 0o755),
    "horizon-state-migrate": (PACKAGE_ROOT / "ops/bin/horizon-state-migrate", 0o755),
    "horizon-telemetry-migrate": (PACKAGE_ROOT / "ops/bin/horizon-telemetry-migrate", 0o755),
    "horizon-jvm-args": (PACKAGE_ROOT / "ops/bin/horizon-jvm-args", 0o755),
    "horizon-memory-drill": (PACKAGE_ROOT / "ops/bin/horizon-memory-drill", 0o755),
    "horizon-phase2-threshold": (PACKAGE_ROOT / "ops/bin/horizon-phase2-threshold", 0o755),
    "horizon-phase2-collect": (PACKAGE_ROOT / "ops/bin/horizon-phase2-collect", 0o755),
    "horizon-phase2-browser-evidence": (PACKAGE_ROOT / "scripts/phase2-browser-evidence.py", 0o755),
    "horizon-phase2-live-acceptance": (PACKAGE_ROOT / "ops/bin/horizon-phase2-live-acceptance", 0o755),
}
SUNLIT_LIBRARIES_LINK = "/srv/game-servers/minecraft-sunlit-cobblemon/libraries"
SUNLIT_LIBRARIES_TARGET = "/opt/game-servers/minecraft-sunlit-cobblemon/libraries"
FIXED_B2_SECRET_PATH = "/etc/game-control/secrets.d/horizon-b2-rclone.conf"
FIXED_RCON_SECRET_PATH = "/etc/game-control/secrets.d/minecraft-rcon-password"

LEGACY_TARGETS = (
    "/etc/game-control/profiles.d/minecraft.toml",
    "/etc/game-control/profiles.d/pz-rising.toml",
    "/etc/game-control/runner.d/minecraft.json",
    "/etc/game-control/runner.d/pz-rising.json",
    "/etc/systemd/system/pz-rising.service",
    "/etc/game-control/secrets.d/crafty-token",
)


class Installer:
    def __init__(
        self,
        root: Path,
        *,
        skip_systemd_verify: bool = False,
        token_source: Path | None = None,
    ):
        self.root = root
        self.skip_systemd_verify = skip_systemd_verify
        # Kept only so older source-only callers fail closed without requiring
        # a coordinated test update. The VM package never reads this path.
        del token_source

    def target(self, path: str | Path) -> Path:
        value = Path(path)
        return self.root / value.relative_to("/") if value.is_absolute() else self.root / value

    def _expected_install_files(self) -> dict[Path, tuple[Path, int]]:
        files: dict[Path, tuple[Path, int]] = {}
        for source in PROFILE_FILES:
            files[self.target(f"/etc/game-control/profiles.d/{source.name}")] = (source, 0o644)
        for source in RUNNER_FILES:
            files[self.target(f"/etc/game-control/runner.d/{source.name}")] = (source, 0o644)
        for source in WEB_FILES:
            files[self.target(f"/opt/game-control/web/{source.name}")] = (source, 0o644)
        for source in UNIT_FILES:
            files[self.target(f"/etc/systemd/system/{source.name}")] = (source, 0o644)
        for source in UNIT_DROPIN_FILES:
            relative = source.relative_to(PACKAGE_ROOT / "ops/systemd")
            files[self.target(f"/etc/systemd/system/{relative}")] = (source, 0o644)
        for source in SLICE_FILES:
            files[self.target(f"/etc/systemd/system/{source.name}")] = (source, 0o644)
        files[self.target("/usr/lib/tmpfiles.d/game-control.conf")] = (TMPFILES, 0o644)
        for name, (source, mode) in HELPER_FILES.items():
            files[self.target(f"/usr/local/libexec/{name}")] = (source, mode)
        files[self.target("/etc/game-control/game-control.toml")] = (ROOT_CONFIG, 0o600)
        files[self.target("/etc/game-control/lazymc/lazymc.toml")] = (LAZYMC_CONFIG, 0o644)
        files[self.target("/etc/game-control/lazymc/server.properties")] = (LAZYMC_SERVER_PROPERTIES, 0o644)
        files[self.target("/etc/nftables.conf")] = (NFTABLES_POLICY, 0o644)
        files[self.target("/etc/systemd/journald@horizon.conf")] = (JOURNAL_BASE, 0o644)
        files[self.target("/usr/local/share/horizon/horizon-private-measurement.conf")] = (
            JOURNAL_MEASUREMENT,
            0o644,
        )
        return files

    def expected_files(self) -> dict[Path, tuple[Path, int]]:
        files = self._expected_install_files()
        files.update(self.runtime_files())
        return files

    def runtime_files(self) -> dict[Path, tuple[Path, int]]:
        # Keep an exact, non-recursive mirror of every package input used by
        # _expected_install_files(). The installed copy of ops/install.py can
        # therefore resolve its own source paths for --check and idempotent
        # re-apply without consulting the original checkout.
        source_files = {
            source: mode for source, mode in self._expected_install_files().values()
        }
        source_files.update({source: 0o644 for source in RUNTIME_SOURCE_FILES})
        for source, relative, mode in RUNTIME_SUPPORT_FILES:
            source_files[source] = mode
        source_files[RUNTIME_VERIFIER] = 0o600
        return {
            self.target(f"/opt/game-control/{source.relative_to(PACKAGE_ROOT).as_posix()}"): (source, mode)
            for source, mode in source_files.items()
        }

    def _runtime_manifest(self) -> Path:
        return self.target(RUNTIME_MANIFEST_PATH)

    @staticmethod
    def _runtime_relative(path: Path, root: Path | None = None) -> str:
        base = root or Path("/opt/game-control")
        return path.relative_to(base).as_posix()

    def _runtime_destination(self, relative: str) -> Path:
        candidate = Path(relative)
        if (
            not relative
            or candidate.is_absolute()
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in relative
        ):
            raise RuntimeError("invalid runtime manifest path")
        return self.target(Path("/opt/game-control") / candidate)

    def _read_runtime_manifest(self) -> dict[str, tuple[str, str]]:
        manifest = self._runtime_manifest()
        if not manifest.exists():
            return {}
        if manifest.is_symlink() or not manifest.is_file():
            raise RuntimeError("runtime manifest is not a regular file")
        entries: dict[str, tuple[str, str]] = {}
        for line in manifest.read_text(encoding="ascii").splitlines():
            if not line:
                continue
            fields = line.split("\t")
            if len(fields) != 4:
                raise RuntimeError("invalid runtime manifest record")
            version, relative, digest, mode = fields
            if version != RUNTIME_MANIFEST_VERSION:
                raise RuntimeError("invalid runtime manifest record")
            self._runtime_destination(relative)
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise RuntimeError("invalid runtime manifest digest")
            if mode not in {"0600", "0644", "0755"}:
                raise RuntimeError("invalid runtime manifest mode")
            if relative in entries:
                raise RuntimeError("duplicate runtime manifest path")
            entries[relative] = (digest, mode)
        return entries

    def _ensure_runtime_parent(self, path: Path) -> None:
        self._validate_runtime_boundary()
        root = self.target("/opt/game-control")
        if root.is_symlink():
            raise RuntimeError("runtime root is symlinked")
        root.mkdir(parents=True, exist_ok=True)
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise RuntimeError("runtime path contains symlink")
            if current.exists() and not current.is_dir():
                raise RuntimeError("runtime path contains non-directory")
            current.mkdir(exist_ok=True)

    def _validate_runtime_boundary(self) -> None:
        for ancestor in reversed(self.root.parents):
            if ancestor.is_symlink():
                raise RuntimeError("install root ancestor is symlinked")
        if self.root.is_symlink():
            raise RuntimeError("install root is symlinked")
        if self.root.exists() and not self.root.is_dir():
            raise RuntimeError("install root is not a directory")
        current = self.root
        for part in ("opt", "game-control"):
            current /= part
            if current.is_symlink():
                raise RuntimeError("runtime path contains symlink")
            if current.exists() and not current.is_dir():
                raise RuntimeError("runtime path contains non-directory")

    def _validate_runtime_parent(self, path: Path) -> None:
        self._validate_runtime_boundary()
        root = self.target("/opt/game-control")
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise RuntimeError("runtime path contains symlink")
            if current.exists() and not current.is_dir():
                raise RuntimeError("runtime path contains non-directory")

    def _remove_stale_runtime_files(self, current: dict[Path, tuple[Path, int]]) -> None:
        previous = self._read_runtime_manifest()
        managed = {self._runtime_relative(path, self.target("/opt/game-control")) for path in current}
        removable: list[Path] = []
        for relative, (digest, _mode) in previous.items():
            if relative in managed:
                continue
            destination = self._runtime_destination(relative)
            self._validate_runtime_parent(destination.parent)
            if destination.is_symlink():
                raise RuntimeError(f"stale managed runtime path requires manual review: {relative}")
            if not destination.exists():
                continue
            if not destination.is_file():
                raise RuntimeError(f"stale managed runtime path requires manual review: {relative}")
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual != digest:
                raise RuntimeError(f"stale managed runtime path changed: {relative}")
            removable.append(destination)
        for destination in removable:
            destination.unlink()

    def _write_runtime_manifest(self, current: dict[Path, tuple[Path, int]]) -> None:
        manifest = self._runtime_manifest()
        self._ensure_runtime_parent(manifest.parent)
        records = []
        runtime_root = self.target("/opt/game-control")
        for destination, (source, mode) in sorted(current.items(), key=lambda item: self._runtime_relative(item[0], runtime_root)):
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            records.append(
                f"{RUNTIME_MANIFEST_VERSION}\t{self._runtime_relative(destination, runtime_root)}\t{digest}\t{mode:04o}\n"
            )
        fd, temporary = tempfile.mkstemp(prefix=".horizon-runtime-manifest.", dir=manifest.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.writelines(records)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, manifest)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _runtime_manifest_problems(self) -> list[str]:
        manifest = self._runtime_manifest()
        try:
            manifest_stat = manifest.lstat()
        except FileNotFoundError:
            return [f"runtime manifest is absent {manifest}"]
        except OSError:
            return [f"runtime manifest is unreadable {manifest}"]
        if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode):
            return [f"runtime manifest is not regular {manifest}"]
        problems: list[str] = []
        if stat.S_IMODE(manifest_stat.st_mode) != 0o600:
            problems.append(f"runtime manifest mode drift {manifest}")
        if manifest_stat.st_uid != 0 or manifest_stat.st_gid != 0:
            problems.append(f"runtime manifest ownership drift {manifest}")
        if manifest_stat.st_nlink != 1:
            problems.append(f"runtime manifest link count drift {manifest}")
        try:
            actual = self._read_runtime_manifest()
        except (OSError, RuntimeError, UnicodeError):
            return problems + [f"runtime manifest is malformed {manifest}"]
        expected: dict[str, tuple[str, str]] = {}
        runtime_root = self.target("/opt/game-control")
        for destination, (source, mode) in self.runtime_files().items():
            expected[self._runtime_relative(destination, runtime_root)] = (
                hashlib.sha256(source.read_bytes()).hexdigest(),
                f"{mode:04o}",
            )
        if set(actual) != set(expected):
            problems.append("runtime manifest path set drift")
        for relative in sorted(set(actual) & set(expected)):
            if actual[relative] != expected[relative]:
                problems.append(f"runtime manifest record drift {relative}")
        return problems

    @staticmethod
    def _preflight_sources(expected: dict[Path, tuple[Path, int]]) -> None:
        for destination, (source, _mode) in expected.items():
            try:
                source_stat = source.lstat()
                if not stat.S_ISREG(source_stat.st_mode):
                    raise RuntimeError(f"package source is not regular: {source}")
                hashlib.sha256(source.read_bytes()).digest()
            except OSError as exc:
                raise RuntimeError(f"package source is unreadable: {source}") from exc

    def expected_links(self) -> dict[Path, str]:
        return {self.target(SUNLIT_LIBRARIES_LINK): SUNLIT_LIBRARIES_TARGET}

    def directories(self) -> tuple[tuple[Path, int, str, str], ...]:
        return (
            (self.target("/etc/game-control"), 0o755, "root", "root"),
            (self.target("/etc/game-control/profiles.d"), 0o755, "root", "root"),
            (self.target("/etc/game-control/runner.d"), 0o755, "root", "root"),
            (self.target("/etc/game-control/secrets.d"), 0o700, "root", "root"),
            (self.target("/etc/game-control/arm"), 0o700, "root", "root"),
            (self.target("/etc/game-control/lazymc"), 0o755, "root", "root"),
            (self.target("/etc/systemd/journald@horizon.conf.d"), 0o700, "root", "root"),
            (self.target("/etc/wireguard"), 0o700, "root", "root"),
            (self.target("/usr/local/share/horizon"), 0o755, "root", "root"),
            # Game units execute helpers from this directory as unprivileged
            # service users, so every path component must remain searchable.
            (self.target("/usr/local/libexec"), 0o755, "root", "root"),
            (self.target("/opt/game-control/web"), 0o755, "root", "root"),
            (self.target("/var/lib/game-control"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control/alerts"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control/horizon-journal"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control/migrations"), 0o700, "root", "root"),
            (self.target("/var/lib/game-control-web"), 0o700, "gamecontrol", "gamecontrol"),
            (self.target("/run/game-control"), 0o755, "root", "root"),
            (self.target("/run/game-slot"), 0o770, "root", "gameslot"),
            (self.target("/opt/game-servers"), 0o755, "root", "root"),
            (self.target("/srv/game-servers"), 0o755, "root", "root"),
            (self.target("/opt/game-servers/minecraft-sunlit-cobblemon"), 0o755, "root", "root"),
            (self.target("/opt/game-servers/minecraft-sunlit-cobblemon/releases"), 0o755, "root", "root"),
            (self.target("/srv/game-servers/minecraft-sunlit-cobblemon"), 0o750, "svc-sunlit", "svc-sunlit"),
            (self.target("/opt/game-servers/terraria-vanilla"), 0o755, "root", "root"),
            (self.target("/opt/game-servers/terraria-tmod"), 0o755, "root", "root"),
            # RestoreService deliberately remaps every extracted member to the
            # existing mutable-root owner. Keep that boundary owned by the
            # fixed game principal so a verified restore remains bootable.
            (self.target("/srv/game-servers/terraria-vanilla"), 0o750, "terraria-vanilla", "terraria-vanilla"),
            (self.target("/srv/game-servers/terraria-tmod"), 0o750, "tmodloader", "tmodloader"),
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
            (self.target("/srv/game-servers/terraria-tmod/logs/tModLoader-Logs"), 0o750, "tmodloader", "tmodloader"),
            (self.target("/var/backups/game-servers/minecraft-sunlit-cobblemon"), 0o700, "root", "root"),
            (self.target("/var/backups/game-servers/terraria-vanilla"), 0o700, "root", "root"),
            (self.target("/var/backups/game-servers/terraria-tmod"), 0o700, "root", "root"),
            (self.target("/var/backups/game-servers"), 0o700, "root", "root"),
        )

    def drift(self) -> list[str]:
        problems: list[str] = []
        for path, mode, user, group in self.directories():
            try:
                directory_stat = path.lstat()
            except FileNotFoundError:
                problems.append(f"missing directory {path}")
                continue
            except OSError:
                problems.append(f"unreadable directory {path}")
                continue
            if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
                problems.append(f"directory is not a directory {path}")
                continue
            if stat.S_IMODE(directory_stat.st_mode) != mode:
                problems.append(f"mode drift {path}")
            if self.root == Path("/") or (user, group) == ("root", "root"):
                try:
                    expected_uid = self._lookup(user)
                    expected_gid = self._lookup(group, group=True)
                except KeyError:
                    problems.append(f"ownership unavailable {path}")
                else:
                    if directory_stat.st_uid != expected_uid or directory_stat.st_gid != expected_gid:
                        problems.append(f"ownership drift {path}")
        for destination, (source, mode) in self.expected_files().items():
            try:
                destination_stat = destination.lstat()
            except FileNotFoundError:
                problems.append(f"missing file {destination}")
                continue
            except OSError:
                problems.append(f"unreadable file {destination}")
                continue
            if not stat.S_ISREG(destination_stat.st_mode):
                problems.append(f"file is not regular {destination}")
                continue
            if destination_stat.st_uid != 0 or destination_stat.st_gid != 0:
                problems.append(f"ownership drift {destination}")
            if destination_stat.st_nlink != 1:
                problems.append(f"link count drift {destination}")
            if stat.S_IMODE(destination_stat.st_mode) != mode:
                problems.append(f"mode drift {destination}")
            try:
                actual_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            except OSError:
                problems.append(f"unreadable file {destination}")
            else:
                if actual_digest != expected_digest:
                    problems.append(f"content drift {destination}")
        problems.extend(self._runtime_manifest_problems())
        secret = self.target(FIXED_B2_SECRET_PATH)
        try:
            secret_stat = secret.lstat()
        except FileNotFoundError:
            problems.append("fixed B2 secret is absent")
        except OSError:
            problems.append("fixed B2 secret cannot be inspected")
        else:
            if stat.S_ISLNK(secret_stat.st_mode):
                problems.append("fixed B2 secret is symlinked")
            elif not stat.S_ISREG(secret_stat.st_mode):
                problems.append("fixed B2 secret is not a regular file")
            else:
                if secret_stat.st_nlink != 1:
                    problems.append("fixed B2 secret has unexpected link count")
                if secret_stat.st_uid != 0 or secret_stat.st_gid != 0:
                    problems.append("fixed B2 secret ownership drift")
                if stat.S_IMODE(secret_stat.st_mode) != 0o600:
                    problems.append("fixed B2 secret mode drift")
        rcon_secret = self.target(FIXED_RCON_SECRET_PATH)
        try:
            rcon_stat = rcon_secret.lstat()
        except FileNotFoundError:
            problems.append("generated RCON secret is absent")
        except OSError:
            problems.append("generated RCON secret cannot be inspected")
        else:
            if stat.S_ISLNK(rcon_stat.st_mode):
                problems.append("generated RCON secret is symlinked")
            elif not stat.S_ISREG(rcon_stat.st_mode):
                problems.append("generated RCON secret is not a regular file")
            elif (
                rcon_stat.st_nlink != 1
                or rcon_stat.st_uid != 0
                or rcon_stat.st_gid != 0
                or stat.S_IMODE(rcon_stat.st_mode) != 0o600
            ):
                problems.append("generated RCON secret ownership or mode drift")
        for destination, target in self.expected_links().items():
            if not destination.is_symlink():
                problems.append(f"missing symlink {destination}")
            else:
                try:
                    actual = os.readlink(destination)
                except OSError:
                    problems.append(f"unreadable symlink {destination}")
                else:
                    if actual != target:
                        problems.append(f"symlink target drift {destination} -> {actual}")
        for legacy in LEGACY_TARGETS:
            path = self.target(legacy)
            if path.exists() or path.is_symlink():
                problems.append(f"legacy target artifact present {path}")
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

    def _ensure_generated_rcon_secret(self) -> None:
        destination = self.target(FIXED_RCON_SECRET_PATH)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(32))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        self._chown(destination, "root", "root")
        os.chmod(destination, 0o600)

    @staticmethod
    def _create_link(destination: Path, target: str) -> None:
        if destination.is_symlink():
            if os.readlink(destination) == target:
                return
            raise RuntimeError(f"refusing to replace unexpected symlink {destination}")
        if destination.exists():
            raise RuntimeError(f"refusing to replace non-symlink {destination}")
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary_path = Path(temporary)
        try:
            os.close(fd)
            temporary_path.unlink()
            os.symlink(target, temporary_path)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _accounts(self) -> None:
        if self.root != Path("/"):
            return
        subprocess.run(["/usr/sbin/groupadd", "--system", "gameslot"], check=False)
        for group in ("gamecontrol", "svc-bore", "svc-lazymc", "svc-sunlit", "terraria-vanilla", "tmodloader"):
            subprocess.run(["/usr/sbin/groupadd", "--system", group], check=False)
        for user, group in (
            ("gameslot", "gameslot"),
            ("gamecontrol", "gamecontrol"),
            ("svc-bore", "svc-bore"),
            ("svc-lazymc", "svc-lazymc"),
            ("svc-sunlit", "svc-sunlit"),
            ("terraria-vanilla", "terraria-vanilla"),
            ("tmodloader", "tmodloader"),
        ):
            subprocess.run(
                ["/usr/sbin/useradd", "--system", "--no-create-home", "--gid", group, "--shell", "/usr/sbin/nologin", user],
                check=False,
            )
            subprocess.run(
                ["/usr/sbin/usermod", "--gid", group, "--shell", "/usr/sbin/nologin", user],
                check=False,
            )
        for user in ("svc-sunlit", "terraria-vanilla", "tmodloader"):
            subprocess.run(
                ["/usr/sbin/usermod", "--append", "--groups", "gameslot", user],
                check=False,
            )

    def apply(self) -> None:
        expected_files = self.expected_files()
        self._preflight_sources(expected_files)
        self._accounts()
        for path, mode, user, group in self.directories():
            self._mkdir(path, mode, user, group)
        runtime_files = self.runtime_files()
        self._remove_stale_runtime_files(runtime_files)
        for destination in runtime_files:
            self._ensure_runtime_parent(destination.parent)
        self._ensure_generated_rcon_secret()
        for destination, target in self.expected_links().items():
            self._create_link(destination, target)
        for profile, user in (
            ("terraria-vanilla", "terraria-vanilla"),
            ("terraria-tmod", "tmodloader"),
        ):
            log = self.target(f"/srv/game-servers/{profile}/logs/server.log")
            log.touch(exist_ok=True)
            os.chmod(log, 0o600)
            self._chown(log, user, user)
        # Application archives contain regular files, not empty directories.
        # Keep the tModLoader bind source represented so restore extraction
        # recreates it before systemd evaluates BindPaths.
        tmod_log_anchor = self.target(
            "/srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor"
        )
        tmod_log_anchor.touch(exist_ok=True)
        os.chmod(tmod_log_anchor, 0o600)
        self._chown(tmod_log_anchor, "tmodloader", "tmodloader")
        for destination, (source, mode) in expected_files.items():
            # Managed systemd units and slices are authoritative package
            # inputs.  Deliberately replace them atomically; preserving an
            # existing unit would allow a reviewed cgroup policy to remain
            # silently stale after reconciliation.
            self._atomic_copy(source, destination, mode)
            self._chown(destination, "root", "root")
        self._write_runtime_manifest(runtime_files)
        if self.root == Path("/"):
            venv = self.target("/opt/game-control/.venv")
            if not venv.exists():
                subprocess.run(["/usr/bin/python3", "-m", "venv", str(venv)], check=True)
            previous_umask = os.umask(0o022)
            try:
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
            finally:
                os.umask(previous_umask)
        if not self.skip_systemd_verify:
            units = [
                str(path)
                for path in self.expected_files()
                if path.name.endswith((".service", ".slice", ".timer"))
            ]
            subprocess.run(["/usr/bin/systemd-analyze", "verify", *units], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    parser.add_argument("--apply", action="store_true", help="apply the package atomically")
    parser.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--skip-systemd-verify", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")
    root = args.root or Path(os.environ.get("GAME_CONTROL_INSTALL_ROOT", "/"))
    installer = Installer(root, skip_systemd_verify=args.skip_systemd_verify)
    if args.check:
        drift = installer.drift()
        if drift:
            for item in drift:
                print(item)
            return 1
        return 0
    installer.apply()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
