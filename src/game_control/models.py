from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ProfileId(StrEnum):
    MINECRAFT = "minecraft"
    MINECRAFT_SUNLIT_COBBLEMON = "minecraft-sunlit-cobblemon"
    PZ_RISING = "pz-rising"
    TERRARIA_VANILLA = "terraria-vanilla"
    TERRARIA_TMOD = "terraria-tmod"
    TERRARIA_TMOD_145_CANDIDATE = "terraria-tmod-145-candidate"


class AdapterKind(StrEnum):
    CRAFTY = "crafty"
    SYSTEMD = "systemd"


class ObservedState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    BLOCKED = "blocked"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class OperationName(StrEnum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    FORCE_STOP = "force_stop"
    COMMAND = "command"
    BACKUP = "backup"
    RESTORE = "restore"
    CLONE_SOURCE = "clone_source"
    CLONE_TARGET = "clone_target"
    UPDATE_CHECK = "update_check"
    UPDATE_APPLY = "update_apply"
    BENCHMARK = "benchmark"


class BackupDestination(StrEnum):
    """Reviewed backup destinations; values are safe RPC selectors only."""

    LOCAL = "local"
    HORIZON_B2 = "horizon-b2"


class NotificationEvent(StrEnum):
    START = "start"
    STOP = "stop"
    FAILED_START = "failed_start"
    CRASH = "crash"
    FORCED_STOP = "forced_stop"
    SLOT_CONFLICT = "slot_conflict"
    LOW_DISK = "low_disk"
    BACKUP_FAILURE = "backup_failure"
    UPDATE_COMPLETE = "update_complete"
    UPDATE_FAILURE = "update_failure"
    IDLE_STOP = "idle_stop"


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PortSpec(StrictFrozenModel):
    protocol: Literal["tcp", "udp"]
    port: int = Field(ge=1, le=65535)
    required: bool = True


class ProcessSpec(StrictFrozenModel):
    executable: Path
    argv_contains: tuple[str, ...] = ()
    ready_log_pattern: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("executable")
    @classmethod
    def absolute_executable(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("executable must be a normalized absolute path")
        return value

    @field_validator("argv_contains")
    @classmethod
    def safe_literals(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not 1 <= len(value) <= 128 or "\x00" in value or "\n" in value for value in values):
            raise ValueError("argv_contains values must be bounded literals")
        return values


class PathSpec(StrictFrozenModel):
    data_roots: tuple[Path, ...] = Field(min_length=1)
    backup_roots: tuple[Path, ...] = ()
    mutable_root: Path
    log_files: tuple[Path, ...] = ()
    backup_root: Path
    install_root: Path
    version_file: Path

    @field_validator("*")
    @classmethod
    def normalized_absolute_paths(cls, value):
        values = value if isinstance(value, tuple) else (value,)
        if any(not path.is_absolute() or ".." in path.parts for path in values):
            raise ValueError("profile paths must be normalized and absolute")
        return value

    @model_validator(mode="after")
    def roots_do_not_overlap_unsafely(self):
        if self.backup_root == self.mutable_root or self.mutable_root in self.backup_root.parents:
            raise ValueError("backup_root must be outside mutable_root")
        if len(set(self.data_roots)) != len(self.data_roots):
            raise ValueError("duplicate profile path")
        if len(set(self.backup_roots)) != len(self.backup_roots):
            raise ValueError("duplicate backup source path")
        return self


class PublicEndpointSpec(StrictFrozenModel):
    host: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
    port: int = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]
    relay_unit: str = Field(pattern=r"^[a-z0-9@_.-]+\.service$")


class UpdateSpec(StrictFrozenModel):
    kind: Literal["manual", "steamcmd_in_place", "release_symlink"]
    app_id: int | None = Field(default=None, ge=1)
    beta: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    download_url: AnyHttpUrl | None = None
    executable_relative_path: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-][A-Za-z0-9_./-]{0,255}$"
    )
    version_command: tuple[str, ...] = ()

    @model_validator(mode="after")
    def fields_match_kind(self):
        if self.download_url is not None and self.download_url.scheme == "http":
            host = self.download_url.host
            try:
                local_http = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local_http = host.lower() == "localhost"
            if not local_http:
                raise ValueError("release update URLs require HTTPS")
        if self.kind == "manual" and any(
            (
                self.app_id,
                self.beta,
                self.download_url,
                self.executable_relative_path,
                self.version_command,
            )
        ):
            raise ValueError("manual update has no executable fields")
        if self.kind == "steamcmd_in_place" and (
            self.app_id is None or self.download_url is not None
        ):
            raise ValueError("steamcmd update requires app_id and no URL")
        if self.kind == "release_symlink" and (
            self.download_url is None or self.executable_relative_path is None
        ):
            raise ValueError("release update requires fixed URL and executable")
        return self


class Profile(StrictFrozenModel):
    id: ProfileId
    display_name: str = Field(min_length=2, max_length=64)
    adapter: AdapterKind
    crafty_server_id: UUID | None = None
    systemd_unit: str | None = Field(default=None, pattern=r"^[a-z0-9@_.-]+\.service$")
    process: ProcessSpec
    ports: tuple[PortSpec, ...] = Field(min_length=1)
    start_timeout_seconds: int = Field(ge=5, le=900)
    stop_timeout_seconds: int = Field(ge=5, le=900)
    health_timeout_seconds: int = Field(ge=5, le=900)
    paths: PathSpec
    min_available_memory_bytes: int = Field(ge=1)
    min_free_disk_bytes: int = Field(ge=1)
    public_endpoint: PublicEndpointSpec | None = None
    operations: frozenset[OperationName] = Field(min_length=1)
    update: UpdateSpec
    notification_events: frozenset[NotificationEvent] = frozenset()
    idle_stop_minutes: int = Field(default=0, ge=0, le=1440)

    @model_validator(mode="after")
    def locator_and_operations_match(self):
        if self.adapter is AdapterKind.CRAFTY:
            if self.crafty_server_id is None or self.systemd_unit is not None:
                raise ValueError("crafty profile requires only crafty_server_id")
        elif self.systemd_unit is None or self.crafty_server_id is not None:
            raise ValueError("systemd profile requires only systemd_unit")
        if OperationName.UPDATE_APPLY in self.operations and self.update.kind == "manual":
            raise ValueError("manual profile cannot apply updates")
        if 0 < self.idle_stop_minutes < 5:
            raise ValueError("idle_stop_minutes must be 0 or between 5 and 1440")
        return self
