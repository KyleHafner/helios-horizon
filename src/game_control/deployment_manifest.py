"""The single, typed declaration of the Horizon deployment package.

This module is deliberately boring: it is stdlib-only, has no import-time
filesystem or process effects, and contains policy rather than a signature.
The installer and the static verifier load it from their own source trees.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import stat
from typing import Iterable


def _relative(value: str, label: str) -> None:
    path = Path(value)
    if not isinstance(value, str) or not value or value in {".", ".."} or path.is_absolute() or "\\" in value:
        raise ValueError(f"invalid {label} path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid {label} path")


def _target(value: str, label: str = "target") -> None:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise ValueError(f"invalid {label} path")
    if any(part in {"", ".", ".."} for part in Path(value).parts):
        raise ValueError(f"invalid {label} path")


def _mode(value: int) -> None:
    if not isinstance(value, int) or value < 0 or value & ~0o7777:
        raise ValueError("invalid mode")


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    id: str
    profile_source: str
    runner_source: str
    unit: str


@dataclass(frozen=True, slots=True)
class FileSpec:
    source: str
    target: str
    mode: int
    owner: str = "root"
    group: str = "root"
    category: str = "static"

    def __post_init__(self) -> None:
        _relative(self.source, "source")
        _target(self.target)
        _mode(self.mode)
        if not self.owner or not self.group:
            raise ValueError("file owner/group must be non-empty")

    def source_path(self, package_root: Path) -> Path:
        return package_root / self.source

    def target_path(self, root: Path = Path("/")) -> Path:
        return _under_root(root, self.target)


@dataclass(frozen=True, slots=True)
class DirectorySpec:
    target: str
    mode: int
    owner: str = "root"
    group: str = "root"

    def __post_init__(self) -> None:
        _target(self.target)
        _mode(self.mode)
        if not self.owner or not self.group:
            raise ValueError("directory owner/group must be non-empty")

    def target_path(self, root: Path = Path("/")) -> Path:
        return _under_root(root, self.target)


@dataclass(frozen=True, slots=True)
class SymlinkSpec:
    target: str
    link_target: str

    def __post_init__(self) -> None:
        _target(self.target)
        _target(self.link_target)

    def target_path(self, root: Path = Path("/")) -> Path:
        return _under_root(root, self.target)


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    name: str
    path: str
    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _target(self.path)
        if not self.name or len(set(self.exact)) != len(self.exact):
            raise ValueError("invalid namespace")


@dataclass(frozen=True, slots=True)
class RetiredSpec:
    paths: tuple[str, ...]
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.paths)) != len(self.paths) or len(set(self.names)) != len(self.names):
            raise ValueError("duplicate retired artifact")
        for path in self.paths:
            _target(path)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str
    target: str
    mode: int = 0o600
    owner: str = "root"
    group: str = "root"

    def __post_init__(self) -> None:
        _target(self.target)
        _mode(self.mode)


@dataclass(frozen=True, slots=True)
class DatabaseSpec:
    name: str
    target: str
    read_only: bool = True

    def __post_init__(self) -> None:
        _target(self.target)


@dataclass(frozen=True, slots=True)
class RuntimeManifestSpec:
    target: str
    version: str
    mode: int = 0o600
    owner: str = "root"
    group: str = "root"

    def __post_init__(self) -> None:
        _target(self.target)
        _mode(self.mode)


@dataclass(frozen=True, slots=True)
class RelayModeSpec:
    name: str
    expectations: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    schema_version: int
    profiles: tuple[ProfileSpec, ...]
    files: tuple[FileSpec, ...]
    directories: tuple[DirectorySpec, ...]
    symlinks: tuple[SymlinkSpec, ...]
    namespaces: tuple[NamespaceSpec, ...]
    retired: RetiredSpec
    secrets: tuple[SecretSpec, ...]
    databases: tuple[DatabaseSpec, ...]
    runtime_manifest: RuntimeManifestSpec
    relay_modes: tuple[RelayModeSpec, ...]
    runtime_sources: tuple[str, ...]
    runtime_support: tuple[FileSpec, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported manifest schema")
        if len({p.id for p in self.profiles}) != len(self.profiles):
            raise ValueError("duplicate profile id")
        if len({f.target for f in self.files}) != len(self.files):
            raise ValueError("duplicate file target")
        if len({d.target for d in self.directories}) != len(self.directories):
            raise ValueError("duplicate directory target")
        if len({s.target for s in self.symlinks}) != len(self.symlinks):
            raise ValueError("duplicate symlink target")
        all_targets = {f.target for f in self.files} | {d.target for d in self.directories} | {s.target for s in self.symlinks}
        if len(all_targets) != len(self.files) + len(self.directories) + len(self.symlinks):
            raise ValueError("deployment target collision")
        for profile in self.profiles:
            if profile.profile_source != f"config/profiles/{profile.id}.toml":
                raise ValueError("profile source mismatch")
            if profile.runner_source != f"config/runner/{profile.id}.json":
                raise ValueError("runner source mismatch")
            if profile.unit != f"{profile.id}.service":
                raise ValueError("profile unit mismatch")
        for source in self.runtime_sources:
            _relative(source, "runtime source")
        if len(set(self.runtime_sources)) != len(self.runtime_sources):
            raise ValueError("duplicate runtime source")
        if len({s.name for s in self.secrets}) != len(self.secrets):
            raise ValueError("duplicate secret name")
        if len({db.name for db in self.databases}) != len(self.databases):
            raise ValueError("duplicate database name")

    @staticmethod
    def _project(records: Iterable[FileSpec | DirectorySpec | SymlinkSpec], root: Path):
        return tuple(replace(record, target=str(record.target_path(root))) for record in records)

    def files_for(self, root: Path = Path("/")) -> tuple[FileSpec, ...]:
        return self._project(self.files, root)

    def directories_for(self, root: Path = Path("/")) -> tuple[DirectorySpec, ...]:
        return self._project(self.directories, root)

    def links_for(self, root: Path = Path("/")) -> tuple[SymlinkSpec, ...]:
        return self._project(self.symlinks, root)

    def secret_metadata_for(self, root: Path = Path("/")) -> tuple[SecretSpec, ...]:
        return tuple(replace(secret, target=str(_under_root(root, secret.target))) for secret in self.secrets)

    def namespace_policy_for(self, root: Path = Path("/")) -> tuple[NamespaceSpec, ...]:
        return tuple(replace(namespace, path=str(_under_root(root, namespace.path))) for namespace in self.namespaces)

    def runtime_files_for(self, root: Path = Path("/")) -> tuple[FileSpec, ...]:
        sources = tuple(
            FileSpec(source=source, target=f"/opt/game-control/{source.removeprefix('src/')}", mode=0o644, category="runtime")
            for source in (*self.runtime_sources, "src/game_control/deployment_manifest.py")
        )
        verifier = FileSpec("scripts/verify-deployed.py", "/opt/game-control/scripts/verify-deployed.py", 0o600, category="runtime")
        return self._project((*self.files, *sources, *self.runtime_support, verifier), root)

    def validate(self, package_root: Path | None = None) -> None:
        """Validate declarations and, when supplied, every source boundary."""
        self.__post_init__()
        if package_root is None:
            return
        for file in (*self.files, *self.runtime_support):
            _source_regular(file.source_path(package_root))
        for source in self.runtime_sources:
            _source_regular(package_root / source)
        _source_regular(package_root / "src/game_control/deployment_manifest.py")
        _source_regular(package_root / "scripts/verify-deployed.py")


def _source_regular(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"package source is not a regular single-link file: {path}")


def _under_root(root: Path, target: str) -> Path:
    return root / target.lstrip("/")


_PROFILES = (
    ProfileSpec("minecraft-sunlit-cobblemon", "config/profiles/minecraft-sunlit-cobblemon.toml", "config/runner/minecraft-sunlit-cobblemon.json", "minecraft-sunlit-cobblemon.service"),
    ProfileSpec("terraria-vanilla", "config/profiles/terraria-vanilla.toml", "config/runner/terraria-vanilla.json", "terraria-vanilla.service"),
    ProfileSpec("terraria-tmod", "config/profiles/terraria-tmod.toml", "config/runner/terraria-tmod.json", "terraria-tmod.service"),
)


def _file(source: str, target: str, mode: int = 0o644, category: str = "static") -> FileSpec:
    return FileSpec(source, target, mode, category=category)


_FILES = (
    *tuple(_file(p.profile_source, f"/etc/game-control/profiles.d/{p.id}.toml") for p in _PROFILES),
    *tuple(_file(p.runner_source, f"/etc/game-control/runner.d/{p.id}.json") for p in _PROFILES),
    *tuple(_file(f"web/{name}", f"/opt/game-control/web/{name}") for name in ("app.js", "commands.js", "index.html", "palette.js", "styles.css")),
    *tuple(_file(f"ops/systemd/{name}", f"/etc/systemd/system/{name}") for name in (
        "game-control-web.service", "game-slotd.service", "horizon-bore-liveness.service", "horizon-bore-liveness.timer",
        "horizon-sunlit-auto-update.service", "horizon-sunlit-auto-update.timer", "lazymc-minecraft.service",
        "bore-minecraft-fenced.service", "horizon-alert-drill@.service", "horizon-alert-notify@.service",
        "horizon-terraria-relay.service", "minecraft-sunlit-cobblemon.service", "terraria-tmod.service", "terraria-vanilla.service")),
    _file("ops/systemd/game-slotd.service.d/io-metrics.conf", "/etc/systemd/system/game-slotd.service.d/io-metrics.conf"),
    _file("ops/systemd/minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf", "/etc/systemd/system/minecraft-sunlit-cobblemon.service.d/gc-telemetry.conf"),
    *tuple(_file(f"ops/systemd/{name}", f"/etc/systemd/system/{name}") for name in ("games.slice", "horizon.slice", "maintenance.slice")),
    _file("ops/tmpfiles/game-control.conf", "/usr/lib/tmpfiles.d/game-control.conf"),
    *tuple(_file("scripts/phase2-browser-evidence.py" if name == "horizon-phase2-browser-evidence" else f"ops/bin/{name}", f"/usr/local/libexec/{name}", 0o644 if name == "horizon_journal.py" else 0o755) for name in (
        "game-slot-run", "game-console-stop", "game-console-command", "game-sunlit-prepare", "game-sunlit-rcon-prepare", "game-sunlit-stop",
        "horizon-capability-issue", "horizon-alert-notify", "horizon-backup-reconcile", "horizon-sunlit-promote", "horizon-sunlit-manifest",
        "horizon-sunlit-stage", "horizon-sunlit-auto-update", "horizon-sunlit-update-rpc", "horizon-bore-liveness", "horizon-lazymc-wake",
        "horizon-journal-evidence", "horizon-journal-finalize", "horizon_journal.py", "horizon-session-revoke-all", "horizon-state-migrate",
        "horizon-telemetry-migrate", "horizon-jvm-args", "horizon-memory-drill", "horizon-phase2-threshold", "horizon-phase2-collect",
        "horizon-phase2-browser-evidence", "horizon-phase2-live-acceptance")),
    _file("config/game-control.toml", "/etc/game-control/game-control.toml", 0o600),
    _file("ops/lazymc/lazymc.toml", "/etc/game-control/lazymc/lazymc.toml"),
    _file("ops/lazymc/server.properties", "/etc/game-control/lazymc/server.properties"),
    _file("ops/nftables/horizon.nft", "/etc/nftables.conf"),
    _file("ops/journald/horizon.conf", "/etc/systemd/journald@horizon.conf"),
    _file("ops/journald/horizon-private-measurement.conf", "/usr/local/share/horizon/horizon-private-measurement.conf"),
)


_DIRECTORIES = (
    ("etc/game-control", 0o755, "root", "root"), ("etc/game-control/profiles.d", 0o755, "root", "root"),
    ("etc/game-control/runner.d", 0o755, "root", "root"), ("etc/game-control/secrets.d", 0o700, "root", "root"),
    ("etc/game-control/arm", 0o700, "root", "root"), ("etc/game-control/lazymc", 0o755, "root", "root"),
    ("etc/systemd/journald@horizon.conf.d", 0o700, "root", "root"), ("etc/wireguard", 0o700, "root", "root"),
    ("usr/local/share/horizon", 0o755, "root", "root"), ("usr/local/libexec", 0o755, "root", "root"),
    ("opt/game-control/web", 0o755, "root", "root"), ("var/lib/game-control", 0o700, "root", "root"),
    ("var/lib/game-control/alerts", 0o700, "root", "root"), ("var/lib/game-control/horizon-journal", 0o700, "root", "root"),
    ("var/lib/game-control/migrations", 0o700, "root", "root"), ("var/lib/game-control-web", 0o700, "gamecontrol", "gamecontrol"),
    ("run/game-control", 0o755, "root", "root"), ("run/game-slot", 0o770, "root", "gameslot"),
    ("opt/game-servers", 0o755, "root", "root"), ("srv/game-servers", 0o755, "root", "root"),
    ("opt/game-servers/minecraft-sunlit-cobblemon", 0o755, "root", "root"), ("opt/game-servers/minecraft-sunlit-cobblemon/releases", 0o755, "root", "root"),
    ("srv/game-servers/minecraft-sunlit-cobblemon", 0o750, "svc-sunlit", "svc-sunlit"),
    ("opt/game-servers/terraria-vanilla", 0o755, "root", "root"), ("opt/game-servers/terraria-tmod", 0o755, "root", "root"),
    ("srv/game-servers/terraria-vanilla", 0o750, "terraria-vanilla", "terraria-vanilla"), ("srv/game-servers/terraria-tmod", 0o750, "tmodloader", "tmodloader"),
    *((f"srv/game-servers/{profile}/{subdir}", 0o750, user, user) for profile, user in (("terraria-vanilla", "terraria-vanilla"), ("terraria-tmod", "tmodloader")) for subdir in ("config", "worlds", "mods", "logs", "backups")),
    *((f"srv/game-servers/{profile}/.local/{suffix}", 0o700, user, user) for profile, user in (("terraria-vanilla", "terraria-vanilla"), ("terraria-tmod", "tmodloader")) for suffix in ("", "share", "share/Terraria")),
    ("srv/game-servers/terraria-tmod/logs/tModLoader-Logs", 0o750, "tmodloader", "tmodloader"),
    ("var/backups/game-servers/minecraft-sunlit-cobblemon", 0o700, "root", "root"), ("var/backups/game-servers/terraria-vanilla", 0o700, "root", "root"),
    ("var/backups/game-servers/terraria-tmod", 0o700, "root", "root"), ("var/backups/game-servers", 0o700, "root", "root"),
)

_RUNTIME_SOURCES = tuple(
    "src/" + name for name in (
        "game_control/__init__.py", "game_control/adapters/__init__.py", "game_control/adapters/base.py", "game_control/adapters/crafty.py", "game_control/adapters/systemd.py", "game_control/runtime/__init__.py", "game_control/runtime/alerts.py", "game_control/runtime/protocols.py", "game_control/runtime/telemetry.py", "game_control/alert_policy.py", "game_control/api.py", "game_control/auth.py", "game_control/backup_reconcile.py", "game_control/backups.py", "game_control/benchmark_safety.py", "game_control/benchmarks.py", "game_control/capability.py", "game_control/capability_evidence.py", "game_control/controller.py", "game_control/db_telemetry.py", "game_control/driver_preflight.py", "game_control/errors.py", "game_control/gc_telemetry.py", "game_control/health.py", "game_control/history_queries.py", "game_control/idle_stop.py", "game_control/interim_maintenance_control.py", "game_control/introspection.py", "game_control/lazymc.py", "game_control/log_follower.py", "game_control/logs.py", "game_control/managed_tuning.py", "game_control/memory_drill.py", "game_control/metrics.py", "game_control/models.py", "game_control/modpack_update.py", "game_control/notifications.py", "game_control/perf.py", "game_control/phase2_collector.py", "game_control/phase2_threshold.py", "game_control/players.py", "game_control/profile.py", "game_control/profile_config.py", "game_control/protocol.py", "game_control/push.py", "game_control/rcon.py", "game_control/rcon_telemetry.py", "game_control/redaction.py", "game_control/root_state.py", "game_control/schedule.py", "game_control/schedule_config.py", "game_control/service_container.py", "game_control/service_wiring.py", "game_control/session_store.py", "game_control/sessions.py", "game_control/slot.py", "game_control/slotd_main.py", "game_control/state_db.py", "game_control/stats_queries.py", "game_control/status.py", "game_control/telemetry_db.py", "game_control/telemetry_migration.py", "game_control/telemetry_sampler.py", "game_control/tick_telemetry.py", "game_control/tps.py", "game_control/updates.py", "game_control/web_db.py", "game_control/web_main.py", "game_control/worlds.py"))

_MANIFEST = DeploymentManifest(
    1, _PROFILES, _FILES, tuple(DirectorySpec("/" + item[0].rstrip("/"), *item[1:]) for item in _DIRECTORIES),
    (SymlinkSpec("/srv/game-servers/minecraft-sunlit-cobblemon/libraries", "/opt/game-servers/minecraft-sunlit-cobblemon/libraries"),),
    (NamespaceSpec("profiles", "/etc/game-control/profiles.d", tuple(f"{p.id}.toml" for p in _PROFILES)), NamespaceSpec("runners", "/etc/game-control/runner.d", tuple(f"{p.id}.json" for p in _PROFILES)), NamespaceSpec("systemd", "/etc/systemd/system", tuple(f.target.removeprefix("/etc/systemd/system/") for f in _FILES if f.target.startswith("/etc/systemd/system/") and "/" not in f.target.removeprefix("/etc/systemd/system/")), ("game-control-", "game-slotd", "horizon-", "lazymc-", "bore-minecraft-fenced")), NamespaceSpec("libexec", "/usr/local/libexec", tuple(f.target.removeprefix("/usr/local/libexec/") for f in _FILES if f.target.startswith("/usr/local/libexec/")), ("game-", "horizon-"))),
    RetiredSpec(("/etc/game-control/profiles.d/minecraft.toml", "/etc/game-control/profiles.d/pz-rising.toml", "/etc/game-control/runner.d/minecraft.json", "/etc/game-control/runner.d/pz-rising.json", "/etc/systemd/system/pz-rising.service", "/etc/game-control/secrets.d/crafty-token"), ("minecraft.toml", "pz-rising.toml", "minecraft.json", "pz-rising.json", "pz-rising.service")),
    (SecretSpec("b2", "/etc/game-control/secrets.d/horizon-b2-rclone.conf"), SecretSpec("rcon", "/etc/game-control/secrets.d/minecraft-rcon-password")),
    (DatabaseSpec("state", "/var/lib/game-control/state.db"), DatabaseSpec("web", "/var/lib/game-control-web/web.db")),
    RuntimeManifestSpec("/opt/game-control/.horizon-runtime-manifest", "1"),
    (RelayModeSpec("private", (("bore-minecraft-fenced.service", False), ("horizon-terraria-relay.service", False))), RelayModeSpec("production", (("bore-minecraft-fenced.service", True), ("horizon-terraria-relay.service", False)))),
    _RUNTIME_SOURCES,
    (FileSpec("pyproject.toml", "/opt/game-control/pyproject.toml", 0o644, category="runtime"), FileSpec("ops/install.py", "/opt/game-control/ops/install.py", 0o755, category="runtime")),
)


def get_manifest() -> DeploymentManifest:
    return _MANIFEST


def manifest_digest() -> str:
    payload = json.dumps(asdict(_MANIFEST), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(payload).hexdigest()
