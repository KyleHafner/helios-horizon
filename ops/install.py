#!/usr/bin/env python3
"""Install the fixed game-control package without touching external services."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_deployment_manifest():
    path = PACKAGE_ROOT / "src/game_control/deployment_manifest.py"
    spec = importlib.util.spec_from_file_location("_horizon_deployment_manifest", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("deployment manifest is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.get_manifest()


_DEPLOYMENT_MANIFEST = _load_deployment_manifest()
_STATIC_SPECS = _DEPLOYMENT_MANIFEST.files
_FILES_BY_TARGET = {spec.target: spec for spec in _STATIC_SPECS}
ACTIVE_PROFILE_IDS = tuple(profile.id for profile in _DEPLOYMENT_MANIFEST.profiles)
PROFILE_FILES = tuple(PACKAGE_ROOT / profile.profile_source for profile in _DEPLOYMENT_MANIFEST.profiles)
RUNNER_FILES = tuple(PACKAGE_ROOT / profile.runner_source for profile in _DEPLOYMENT_MANIFEST.profiles)
WEB_FILES = tuple(PACKAGE_ROOT / spec.source for spec in _STATIC_SPECS if spec.target.startswith("/opt/game-control/web/"))
UNIT_FILES = tuple(PACKAGE_ROOT / spec.source for spec in _STATIC_SPECS if spec.target.startswith("/etc/systemd/system/") and "/" not in spec.target.removeprefix("/etc/systemd/system/" ) and not spec.target.endswith(".slice"))
UNIT_DROPIN_FILES = tuple(PACKAGE_ROOT / spec.source for spec in _STATIC_SPECS if spec.target.startswith("/etc/systemd/system/") and "/" in spec.target.removeprefix("/etc/systemd/system/"))
SLICE_FILES = tuple(PACKAGE_ROOT / spec.source for spec in _STATIC_SPECS if spec.target.startswith("/etc/systemd/system/") and spec.target.endswith(".slice"))
TMPFILES = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/usr/lib/tmpfiles.d/game-control.conf")
ROOT_CONFIG = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/etc/game-control/game-control.toml")
LAZYMC_CONFIG = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/etc/game-control/lazymc/lazymc.toml")
LAZYMC_SERVER_PROPERTIES = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/etc/game-control/lazymc/server.properties")
NFTABLES_POLICY = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/etc/nftables.conf")
JOURNAL_BASE = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/etc/systemd/journald@horizon.conf")
JOURNAL_MEASUREMENT = PACKAGE_ROOT / next(spec.source for spec in _STATIC_SPECS if spec.target == "/usr/local/share/horizon/horizon-private-measurement.conf")
RUNTIME_SOURCE_FILES = tuple(PACKAGE_ROOT / source for source in (*_DEPLOYMENT_MANIFEST.runtime_sources, "src/game_control/deployment_manifest.py"))
RUNTIME_VERIFIER = PACKAGE_ROOT / "scripts/verify-deployed.py"
RUNTIME_SUPPORT_FILES = tuple((PACKAGE_ROOT / spec.source, spec.source, spec.mode) for spec in _DEPLOYMENT_MANIFEST.runtime_support)
RUNTIME_MANIFEST_PATH = _DEPLOYMENT_MANIFEST.runtime_manifest.target
RUNTIME_MANIFEST_VERSION = _DEPLOYMENT_MANIFEST.runtime_manifest.version
HELPER_FILES = {spec.target.removeprefix("/usr/local/libexec/"): (PACKAGE_ROOT / spec.source, spec.mode) for spec in _STATIC_SPECS if spec.target.startswith("/usr/local/libexec/")}
SUNLIT_LIBRARIES_LINK = _DEPLOYMENT_MANIFEST.symlinks[0].target
SUNLIT_LIBRARIES_TARGET = _DEPLOYMENT_MANIFEST.symlinks[0].link_target
FIXED_B2_SECRET_PATH = next(secret.target for secret in _DEPLOYMENT_MANIFEST.secrets if secret.name == "b2")
FIXED_RCON_SECRET_PATH = next(secret.target for secret in _DEPLOYMENT_MANIFEST.secrets if secret.name == "rcon")
LEGACY_TARGETS = _DEPLOYMENT_MANIFEST.retired.paths


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
        return {
            Path(spec.target): (spec.source_path(PACKAGE_ROOT), spec.mode)
            for spec in _DEPLOYMENT_MANIFEST.files_for(self.root)
        }

    def expected_files(self) -> dict[Path, tuple[Path, int]]:
        files = self._expected_install_files()
        files.update(self.runtime_files())
        return files

    def runtime_files(self) -> dict[Path, tuple[Path, int]]:
        # Keep an exact, non-recursive mirror of every package input used by
        # _expected_install_files(). The installed copy of ops/install.py can
        # therefore resolve its own source paths for --check and idempotent
        # re-apply without consulting the original checkout.
        return {
            Path(spec.target): (spec.source_path(PACKAGE_ROOT), spec.mode)
            for spec in _DEPLOYMENT_MANIFEST.runtime_files_for(self.root)
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
        try:
            manifest_stat = manifest.lstat()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RuntimeError("runtime manifest is unreadable") from exc
        if stat.S_ISLNK(manifest_stat.st_mode) or not stat.S_ISREG(manifest_stat.st_mode):
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
        try:
            self._validate_runtime_boundary()
            root = self.target("/opt/game-control")
            self._ensure_directory_chain(root, "runtime root")
            self._ensure_directory_chain(path, "runtime path")
        except RuntimeError as exc:
            if "symlinked" in str(exc):
                raise RuntimeError("runtime path contains symlink") from exc
            if "not a directory" in str(exc):
                raise RuntimeError("runtime path contains non-directory") from exc
            raise

    def _validate_runtime_boundary(self) -> None:
        self._validate_existing_chain(self.root, "install root")
        current = self.root
        for part in ("opt", "game-control"):
            current /= part
            self._validate_existing_chain(current, "runtime path")

    def _validate_runtime_parent(self, path: Path) -> None:
        self._validate_runtime_boundary()
        self._validate_existing_chain(path, "runtime path")

    @staticmethod
    def _absolute_lexical(path: Path) -> Path:
        """Return an absolute path without resolving symlinks."""
        return path if path.is_absolute() else Path.cwd() / path

    def _validate_existing_chain(self, path: Path, label: str) -> None:
        candidate = self._absolute_lexical(Path(path))
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise RuntimeError(f"{label} is unreadable: {current}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"{label} is symlinked: {current}")
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"{label} is not a directory: {current}")

    def _ensure_directory_chain(self, path: Path, label: str) -> None:
        candidate = self._absolute_lexical(Path(path))
        current = Path(candidate.anchor)
        for part in candidate.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                try:
                    info = current.lstat()
                except OSError as exc:
                    raise RuntimeError(f"{label} is unreadable: {current}") from exc
            except OSError as exc:
                raise RuntimeError(f"{label} is unreadable: {current}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"{label} is symlinked: {current}")
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"{label} is not a directory: {current}")

    def _managed_parent_paths(
        self,
        expected_files: dict[Path, tuple[Path, int]],
        runtime_files: dict[Path, tuple[Path, int]],
    ) -> set[Path]:
        parents = {self.root}
        parents.update(path.parent for path in expected_files)
        parents.update(path.parent for path in runtime_files)
        parents.update(path for path, _mode, _user, _group in self.directories())
        parents.update(path.parent for path in self.expected_links())
        parents.update(
            self.target(path).parent
            for path in (
                "/srv/game-servers/terraria-vanilla/logs/server.log",
                "/srv/game-servers/terraria-tmod/logs/server.log",
                "/srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor",
                FIXED_B2_SECRET_PATH,
                FIXED_RCON_SECRET_PATH,
                RUNTIME_MANIFEST_PATH,
            )
        )
        return parents

    def _validate_managed_destination(self, path: Path, label: str) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(f"{label} is unreadable: {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"{label} is symlinked: {path}")
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{label} is not a regular file: {path}")
        if info.st_nlink != 1:
            raise RuntimeError(f"{label} has unexpected link count: {path}")

    def _validate_secret_destination(self, path: Path, label: str) -> None:
        self._validate_managed_destination(path, label)

    def _validate_link_destination(self, path: Path, target: str) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError(f"managed link is unreadable: {path}") from exc
        if not stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"managed link destination is not a symlink: {path}")
        try:
            actual = os.readlink(path)
        except OSError as exc:
            raise RuntimeError(f"managed link is unreadable: {path}") from exc
        if actual != target:
            raise RuntimeError(f"refusing to replace unexpected symlink {path}")

    def _validate_boundaries(
        self,
        expected_files: dict[Path, tuple[Path, int]],
        runtime_files: dict[Path, tuple[Path, int]],
    ) -> None:
        for path in sorted(self._managed_parent_paths(expected_files, runtime_files), key=str):
            self._validate_existing_chain(path, "managed parent")

    def _validate_file_directory_collisions(
        self,
        expected_files: dict[Path, tuple[Path, int]],
    ) -> None:
        directory_paths = {
            path for path, _mode, _user, _group in self.directories()
        }
        for file_path in sorted(expected_files, key=str):
            if any(
                file_path == directory_path
                or file_path in directory_path.parents
                for directory_path in directory_paths
            ):
                raise RuntimeError(f"managed file/directory target collision: {file_path}")

    def _preflight_install(
        self,
        expected_files: dict[Path, tuple[Path, int]],
        runtime_files: dict[Path, tuple[Path, int]],
    ) -> None:
        self._validate_existing_chain(self.root, "install root")
        self._validate_file_directory_collisions(expected_files)
        self._validate_boundaries(expected_files, runtime_files)
        for destination in sorted(expected_files, key=str):
            self._validate_managed_destination(destination, "managed destination")
        self._validate_managed_destination(self._runtime_manifest(), "runtime manifest")
        self._read_runtime_manifest()
        self._validate_secret_destination(self.target(FIXED_B2_SECRET_PATH), "fixed B2 secret")
        self._validate_secret_destination(self.target(FIXED_RCON_SECRET_PATH), "generated RCON secret")
        for destination, target in self.expected_links().items():
            self._validate_link_destination(destination, target)
        for path in (
            self.target("/srv/game-servers/terraria-vanilla/logs/server.log"),
            self.target("/srv/game-servers/terraria-tmod/logs/server.log"),
            self.target("/srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor"),
        ):
            self._validate_managed_destination(path, "managed anchor")
        self._stale_runtime_removals(runtime_files)
        self._preflight_sources(expected_files)

    def _stale_runtime_removals(self, current: dict[Path, tuple[Path, int]]) -> list[Path]:
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
            try:
                destination_stat = destination.lstat()
            except OSError as exc:
                raise RuntimeError(f"stale managed runtime path requires manual review: {relative}") from exc
            if destination_stat.st_nlink != 1:
                raise RuntimeError(f"stale managed runtime path has unexpected link count: {relative}")
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual != digest:
                raise RuntimeError(f"stale managed runtime path changed: {relative}")
            removable.append(destination)
        return removable

    def _remove_stale_runtime_files(self, current: dict[Path, tuple[Path, int]]) -> None:
        removable = self._stale_runtime_removals(current)
        for destination in removable:
            self._validate_runtime_parent(destination.parent)
            self._validate_managed_destination(destination, "stale managed runtime path")
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
                if source_stat.st_nlink != 1:
                    raise RuntimeError(f"package source has unexpected link count: {source}")
                hashlib.sha256(source.read_bytes()).digest()
            except OSError as exc:
                raise RuntimeError(f"package source is unreadable: {source}") from exc

    def expected_links(self) -> dict[Path, str]:
        return {spec.target_path(self.root): spec.link_target for spec in _DEPLOYMENT_MANIFEST.symlinks}

    def directories(self) -> tuple[tuple[Path, int, str, str], ...]:
        return tuple(
            (spec.target_path(self.root), spec.mode, spec.owner, spec.group)
            for spec in _DEPLOYMENT_MANIFEST.directories
        )
    def drift(self) -> list[str]:
        problems: list[str] = []
        try:
            expected_files = self.expected_files()
            runtime_files = self.runtime_files()
            self._validate_boundaries(expected_files, runtime_files)
        except RuntimeError as exc:
            return [str(exc)]
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
        self._ensure_directory_chain(path, "managed directory")
        os.chmod(path, mode)
        self._chown(path, user, group)

    def _atomic_copy(self, source: Path, destination: Path, mode: int) -> None:
        self._validate_existing_chain(destination.parent, "managed parent")
        self._validate_managed_destination(destination, "managed destination")
        self._ensure_directory_chain(destination.parent, "managed parent")
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                with source.open("rb") as source_stream:
                    shutil.copyfileobj(source_stream, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_path, mode)
            self._validate_existing_chain(destination.parent, "managed parent")
            self._validate_managed_destination(destination, "managed destination")
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _ensure_generated_rcon_secret(self) -> None:
        destination = self.target(FIXED_RCON_SECRET_PATH)
        self._validate_existing_chain(destination.parent, "secret parent")
        try:
            destination_stat = destination.lstat()
        except FileNotFoundError:
            destination_stat = None
        except OSError as exc:
            raise RuntimeError(f"generated RCON secret is unreadable: {destination}") from exc
        if destination_stat is not None:
            if stat.S_ISLNK(destination_stat.st_mode):
                raise RuntimeError(f"generated RCON secret is symlinked: {destination}")
            if not stat.S_ISREG(destination_stat.st_mode):
                raise RuntimeError(f"generated RCON secret is not a regular file: {destination}")
            if destination_stat.st_nlink != 1:
                raise RuntimeError(f"generated RCON secret has unexpected link count: {destination}")
            return
        self._ensure_directory_chain(destination.parent, "secret parent")
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

    def _create_link(self, destination: Path, target: str) -> None:
        self._validate_existing_chain(destination.parent, "link parent")
        self._validate_link_destination(destination, target)
        if destination.is_symlink():
            return
        self._ensure_directory_chain(destination.parent, "link parent")
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary_path = Path(temporary)
        try:
            os.close(fd)
            temporary_path.unlink()
            os.symlink(target, temporary_path)
            self._validate_existing_chain(destination.parent, "link parent")
            self._validate_link_destination(destination, target)
            os.replace(temporary_path, destination)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _ensure_managed_file(self, destination: Path, mode: int, user: str, group: str) -> None:
        self._validate_existing_chain(destination.parent, "managed parent")
        try:
            info = destination.lstat()
        except FileNotFoundError:
            info = None
        except OSError as exc:
            raise RuntimeError(f"managed file is unreadable: {destination}") from exc
        if info is None:
            self._ensure_directory_chain(destination.parent, "managed parent")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(destination, flags, mode)
            os.close(fd)
        else:
            self._validate_managed_destination(destination, "managed file")
        os.chmod(destination, mode)
        self._chown(destination, user, group)

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
        runtime_files = self.runtime_files()
        self._preflight_install(expected_files, runtime_files)
        self._accounts()
        for path, mode, user, group in self.directories():
            self._mkdir(path, mode, user, group)
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
            self._ensure_managed_file(log, 0o600, user, user)
        # Application archives contain regular files, not empty directories.
        # Keep the tModLoader bind source represented so restore extraction
        # recreates it before systemd evaluates BindPaths.
        tmod_log_anchor = self.target(
            "/srv/game-servers/terraria-tmod/logs/tModLoader-Logs/.horizon-restore-anchor"
        )
        self._ensure_managed_file(tmod_log_anchor, 0o600, "tmodloader", "tmodloader")
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
