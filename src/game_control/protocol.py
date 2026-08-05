"""Closed, safe RPC contract for the root game-control broker.

The protocol deliberately contains no paths, commands, credentials, URLs, or
unvalidated upstream values.  It is the boundary shared by the web client and
the privileged controller.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .models import (
    AdapterKind,
    HealthState,
    NotificationEvent,
    ObservedState,
    OperationName,
    ProfileId,
)

MAX_REQUEST_BYTES = 64 * 1024


class RpcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PageOptions(RpcModel):
    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=100, ge=1, le=500)


class LogOptions(PageOptions):
    limit: int = Field(default=500, ge=1, le=5000)
    severity: Literal["all", "debug", "info", "warning", "error"] = "all"
    since: datetime | None = None
    until: datetime | None = None

    @field_validator("since", "until")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("log range datetimes must include a timezone")
        return value


class SwitchOptions(RpcModel):
    create_backup: bool = False
    force_after_timeout: bool = False
    rollback_on_failure: bool = True


class GetStatus(RpcModel):
    kind: Literal["get_status"]
    refresh: bool = False


class GetPerf(RpcModel):
    kind: Literal["get_perf"]


class GetProfiles(RpcModel):
    kind: Literal["get_profiles"]


class GetLogs(RpcModel):
    kind: Literal["get_logs"]
    profile_id: ProfileId
    page: LogOptions


class ListBackups(RpcModel):
    kind: Literal["list_backups"]
    profile_id: ProfileId
    page: PageOptions


class ListEvents(RpcModel):
    kind: Literal["list_events"]
    page: PageOptions


class GetStatsSummary(RpcModel):
    kind: Literal["get_stats_summary"]
    profile_id: ProfileId
    days: int | None = Field(default=None, ge=1, le=3650)


class GetStatsHeatmap(RpcModel):
    kind: Literal["get_stats_heatmap"]
    profile_id: ProfileId
    days: int = Field(default=90, ge=1, le=365)


class GetStatsTps(RpcModel):
    kind: Literal["get_stats_tps"]
    profile_id: ProfileId
    window: Literal["1h", "6h", "24h"] = "6h"


class GetProfileConfig(RpcModel):
    kind: Literal["get_profile_config"]
    profile_id: ProfileId


class SetProfileConfig(RpcModel):
    kind: Literal["set_profile_config"]
    profile_id: ProfileId
    changes: dict[str, Any] = Field(min_length=1, max_length=16)


class ProfileConfigEntry(RpcModel):
    key: str
    value: Any | None
    configured: bool | None = None
    type: Literal["str", "int", "enum", "bool"]
    bounds: dict[str, Any]
    restart_required: bool


class ProfileConfigResponse(RpcModel):
    profile_id: ProfileId
    settings: tuple[ProfileConfigEntry, ...]
    changed: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()


class ListAudit(RpcModel):
    kind: Literal["list_audit"]
    page: PageOptions


class Start(RpcModel):
    kind: Literal["start"]
    profile_id: ProfileId


class Stop(RpcModel):
    kind: Literal["stop"]
    profile_id: ProfileId


class SetIdleStop(RpcModel):
    kind: Literal["set_idle_stop"]
    profile_id: ProfileId
    minutes: int = Field(ge=0, le=1440)

    @field_validator("minutes")
    @classmethod
    def validate_minutes(cls, value: int) -> int:
        if 0 < value < 5:
            raise ValueError("idle stop must be disabled or between 5 and 1440 minutes")
        return value


class Restart(RpcModel):
    kind: Literal["restart"]
    profile_id: ProfileId
    confirmation_id: str | None = None


class Command(RpcModel):
    kind: Literal["command"]
    profile_id: ProfileId
    command: str = Field(max_length=512)

    @field_validator("command", mode="before")
    @classmethod
    def validate_command(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("command must be text")
        if len(value) > 512:
            raise ValueError("command is too long")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("command contains a control character")
        normalized = value.strip(" ")
        if not normalized:
            raise ValueError("command must not be empty")
        return normalized


class PrepareSwitch(RpcModel):
    kind: Literal["prepare_switch"]
    current_profile_id: ProfileId
    target_profile_id: ProfileId
    options: SwitchOptions


class ConfirmSwitch(RpcModel):
    kind: Literal["confirm_switch"]
    confirmation_id: str = Field(min_length=32, max_length=128)


class PrepareForceStop(RpcModel):
    kind: Literal["prepare_force_stop"]
    profile_id: ProfileId


class ConfirmForceStop(RpcModel):
    kind: Literal["confirm_force_stop"]
    confirmation_id: str = Field(min_length=32, max_length=128)


class CreateBackup(RpcModel):
    kind: Literal["create_backup"]
    profile_id: ProfileId
    protected: bool = False


class PrepareRestore(RpcModel):
    kind: Literal["prepare_restore"]
    profile_id: ProfileId
    backup_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class ConfirmRestore(RpcModel):
    kind: Literal["confirm_restore"]
    confirmation_id: str = Field(min_length=32, max_length=128)


class PrepareWorldClone(RpcModel):
    kind: Literal["prepare_world_clone"]
    source_world_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    destination_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")


class ConfirmWorldClone(RpcModel):
    kind: Literal["confirm_world_clone"]
    confirmation_id: str = Field(min_length=32, max_length=128)


class CheckUpdate(RpcModel):
    kind: Literal["check_update"]
    profile_id: ProfileId


class PrepareUpdate(RpcModel):
    kind: Literal["prepare_update"]
    profile_id: ProfileId


class ConfirmUpdate(RpcModel):
    kind: Literal["confirm_update"]
    confirmation_id: str = Field(min_length=32, max_length=128)


class GetNotificationConfig(RpcModel):
    kind: Literal["get_notification_config"]
    profile_id: ProfileId


class SetNotificationRule(RpcModel):
    kind: Literal["set_notification_rule"]
    profile_id: ProfileId
    event: NotificationEvent
    enabled: bool


class TestNotification(RpcModel):
    kind: Literal["test_notification"]
    channel: Literal["discord", "telegram"]
    profile_id: ProfileId


RpcAction: TypeAlias = Annotated[
    GetStatus
    | GetPerf
    | GetProfiles
    | GetLogs
    | ListBackups
    | ListEvents
    | GetStatsSummary
    | GetStatsHeatmap
    | GetStatsTps
    | GetProfileConfig
    | SetProfileConfig
    | ListAudit
    | Start
    | Stop
    | SetIdleStop
    | Restart
    | Command
    | PrepareSwitch
    | ConfirmSwitch
    | PrepareForceStop
    | ConfirmForceStop
    | CreateBackup
    | PrepareRestore
    | ConfirmRestore
    | PrepareWorldClone
    | ConfirmWorldClone
    | CheckUpdate
    | PrepareUpdate
    | ConfirmUpdate
    | GetNotificationConfig
    | SetNotificationRule
    | TestNotification,
    Field(discriminator="kind"),
]


class RpcRequest(RpcModel):
    request_id: UUID
    actor: str = Field(pattern=r"^[A-Za-z0-9@._-]{1,128}$")
    action: RpcAction


class ErrorCode(StrEnum):
    SLOT_CONFLICT = "slot_conflict"
    PROFILE_RESERVED = "profile_reserved"
    LOW_DISK = "low_disk"
    LOW_MEMORY = "low_memory"
    REQUIRED_FILE_MISSING = "required_file_missing"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    START_TIMEOUT = "start_timeout"
    HEALTH_FAILED = "health_failed"
    GRACE_TIMEOUT = "grace_timeout"
    BACKUP_FAILED = "backup_failed"
    RESTORE_FAILED = "restore_failed"
    UPDATE_FAILED = "update_failed"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    REQUEST_ID_CONFLICT = "request_id_conflict"
    UNAUTHORIZED_PEER = "unauthorized_peer"
    INVALID_REQUEST = "invalid_request"
    INVALID_STATE = "invalid_state"
    INTERNAL_ERROR = "internal_error"


class SafeDetails(RpcModel):
    profile_id: ProfileId | None = None
    current_owner: ProfileId | None = None
    target_profile_id: ProfileId | None = None
    expected_timeout_seconds: int | None = Field(default=None, ge=0, le=1800)
    last_backup_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
    allowed_actions: tuple[OperationName, ...] = ()
    incident_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    retry_after_seconds: int | None = Field(default=None, ge=0, le=3600)


class RpcError(RpcModel):
    code: ErrorCode
    message: str = Field(max_length=512)
    retryable: bool
    details: SafeDetails | None = None


class PublicEndpoint(RpcModel):
    host: str
    port: int
    protocol: Literal["tcp", "udp"]
    reachable: bool | None


class PublicProfile(RpcModel):
    id: ProfileId
    display_name: str
    adapter: AdapterKind
    operations: frozenset[OperationName]
    public_endpoint: PublicEndpoint | None
    idle_stop_minutes: int = Field(ge=0, le=1440)


class ProfileStatus(RpcModel):
    profile_id: ProfileId
    state: ObservedState
    health: HealthState
    slot_owner: ProfileId | None
    active_job_id: str | None
    pid: int | None
    started_at: datetime | None
    uptime_seconds: int | None
    cpu_percent: float | None
    rss_bytes: int | None
    players_online: int | None
    installed_version: str | None
    restart_required: bool
    required_ports_ready: bool
    disk_free_bytes: int | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None


class StatusSnapshot(RpcModel):
    generation: int = Field(ge=0)
    observed_at: datetime
    profiles: tuple[ProfileStatus, ...]
    initializing: bool = False


class PerfAggregate(RpcModel):
    count: int = Field(ge=0)
    avg_ms: float | None = Field(default=None, ge=0)
    p95_ms: float | None = Field(default=None, ge=0)
    max_ms: float | None = Field(default=None, ge=0)


class PerfSnapshot(RpcModel):
    cycle: PerfAggregate
    rpc: PerfAggregate


class LogLine(RpcModel):
    timestamp: datetime
    severity: Literal["debug", "info", "warning", "error"]
    message: str = Field(max_length=8192)


class LogPage(RpcModel):
    items: tuple[LogLine, ...]
    next_cursor: str | None


class BackupSummary(RpcModel):
    id: str
    profile_id: ProfileId
    created_at: datetime
    size_bytes: int = Field(ge=0)
    verified: bool
    protected: bool


class BackupPage(RpcModel):
    items: tuple[BackupSummary, ...]
    next_cursor: str | None


class EventSummary(RpcModel):
    id: str
    timestamp: datetime
    profile_id: ProfileId | None
    code: str = Field(max_length=64)
    message: str = Field(max_length=512)


class EventPage(RpcModel):
    items: tuple[EventSummary, ...]
    next_cursor: str | None


class AuditSummary(RpcModel):
    id: str
    timestamp: datetime
    actor: str
    action: str
    profile_id: ProfileId | None
    result: Literal["accepted", "succeeded", "failed", "rejected"]
    error_code: ErrorCode | None
    detail: str = Field(max_length=512)


class AuditPage(RpcModel):
    items: tuple[AuditSummary, ...]
    next_cursor: str | None


class JobAccepted(RpcModel):
    job_id: str
    state: Literal["accepted", "running"]


class ConfirmationBase(RpcModel):
    confirmation_id: str
    expires_at: datetime
    summary_hash: str
    state_generation: int = Field(ge=0)


class SwitchConfirmation(ConfirmationBase):
    action: Literal["switch"]
    source_profile_id: ProfileId
    target_profile_id: ProfileId
    create_backup: bool
    force_after_timeout: bool
    rollback_on_failure: bool


class ForceStopConfirmation(ConfirmationBase):
    action: Literal["force_stop"]
    profile_id: ProfileId


class RestoreConfirmation(ConfirmationBase):
    action: Literal["restore"]
    profile_id: ProfileId
    backup_id: str


class WorldCloneConfirmation(ConfirmationBase):
    action: Literal["world_clone"]
    source_profile_id: Literal[ProfileId.TERRARIA_VANILLA]
    target_profile_id: Literal[ProfileId.TERRARIA_TMOD]
    source_world_id: str
    destination_name: str


class UpdateConfirmation(ConfirmationBase):
    action: Literal["update"]
    profile_id: ProfileId
    installed_version: str | None
    available_version: str | None


ConfirmationSummary: TypeAlias = Annotated[
    SwitchConfirmation
    | ForceStopConfirmation
    | RestoreConfirmation
    | WorldCloneConfirmation
    | UpdateConfirmation,
    Field(discriminator="action"),
]


class UpdateStatus(RpcModel):
    profile_id: ProfileId
    strategy: Literal["manual", "steamcmd_in_place", "release_symlink"]
    installed_version: str | None
    available_version: str | None
    restart_required: bool
    apply_supported: bool


class NotificationTarget(RpcModel):
    channel: Literal["discord", "telegram"]
    configured: bool
    label: str | None


class NotificationConfig(RpcModel):
    profile_id: ProfileId
    targets: tuple[NotificationTarget, ...]
    rules: dict[NotificationEvent, bool]


RpcResult: TypeAlias = (
    StatusSnapshot
    | PerfSnapshot
    | tuple[PublicProfile, ...]
    | LogPage
    | BackupPage
    | EventPage
    | AuditPage
    | JobAccepted
    | ConfirmationSummary
    | UpdateStatus
    | NotificationConfig
    | ProfileConfigResponse
    | dict[str, Any]
)


class RpcSuccess(RpcModel):
    request_id: UUID
    ok: Literal[True] = True
    result: RpcResult


class RpcFailure(RpcModel):
    request_id: UUID
    ok: Literal[False] = False
    error: RpcError


RpcResponse: TypeAlias = Annotated[
    RpcSuccess | RpcFailure, Field(discriminator="ok")
]


_REQUEST_ADAPTER = TypeAdapter(RpcRequest)
_RESPONSE_ADAPTER = TypeAdapter(RpcResponse)


def parse_request_line(data: bytes) -> RpcRequest:
    """Parse exactly one bounded JSON object from a newline-delimited frame."""

    if not isinstance(data, bytes):
        raise TypeError("request frame must be bytes")
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    if not data.endswith(b"\n"):
        raise ValueError("request must end with newline")
    body = data[:-1]
    if not body or b"\n" in body or b"\r" in body:
        raise ValueError("request must contain one JSON object")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON request") from exc
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    try:
        return _REQUEST_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError("invalid RPC request") from exc


def response_from_json(data: bytes) -> RpcSuccess | RpcFailure:
    """Validate a response envelope before writing it to a socket."""

    return _RESPONSE_ADAPTER.validate_json(data)


def response_json(response: RpcSuccess | RpcFailure) -> bytes:
    return _RESPONSE_ADAPTER.dump_json(response) + b"\n"


def success(request_id: UUID, result: RpcResult) -> RpcSuccess:
    return RpcSuccess(request_id=request_id, result=result)


def failure(
    request_id: UUID,
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    details: SafeDetails | None = None,
) -> RpcFailure:
    return RpcFailure(
        request_id=request_id,
        error=RpcError(code=code, message=message[:512], retryable=retryable, details=details),
    )


__all__ = [
    "MAX_REQUEST_BYTES",
    "RpcModel",
    "PageOptions",
    "LogOptions",
    "SwitchOptions",
    "RpcAction",
    "Command",
    "RpcRequest",
    "RpcResponse",
    "RpcSuccess",
    "RpcFailure",
    "RpcResult",
    "ErrorCode",
    "SafeDetails",
    "RpcError",
    "PublicEndpoint",
    "PublicProfile",
    "ProfileStatus",
    "StatusSnapshot",
    "PerfAggregate",
    "PerfSnapshot",
    "LogLine",
    "LogPage",
    "BackupSummary",
    "BackupPage",
    "EventSummary",
    "EventPage",
    "GetStatsSummary",
    "GetStatsHeatmap",
    "GetStatsTps",
    "GetProfileConfig",
    "SetProfileConfig",
    "AuditSummary",
    "AuditPage",
    "JobAccepted",
    "ConfirmationSummary",
    "SwitchConfirmation",
    "ForceStopConfirmation",
    "RestoreConfirmation",
    "WorldCloneConfirmation",
    "UpdateConfirmation",
    "UpdateStatus",
    "NotificationTarget",
    "NotificationConfig",
    "ProfileConfigEntry",
    "ProfileConfigResponse",
    "parse_request_line",
    "response_from_json",
    "response_json",
    "success",
    "failure",
]
