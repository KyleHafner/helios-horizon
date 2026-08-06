"""Small, fail-closed capability boundary for external Horizon agents.

The capability surface is deliberately separate from the operator session
surface.  Tokens are bearer credentials, so only a hash and fixed metadata are
ever persisted.  The public request model has no profile, unit, path, command,
or confirmation fields; those values are selected by this module.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import ObservedState, ProfileId
from .protocol import (
    ErrorCode,
    GetStatsTps,
    GetStatus,
    JobAccepted,
    RpcFailure,
    RpcRequest,
    RpcResponse,
    RpcSuccess,
    Start,
    StatusSnapshot,
)


CAPABILITY_PATH_PREFIX = "/api/v1/capability"
CAPABILITY_MAX_BODY_BYTES = 4096
CAPABILITY_MAX_RESPONSE_BYTES = 64 * 1024
CAPABILITY_MAX_TTL = timedelta(days=30)
CAPABILITY_DEFAULT_TTL = timedelta(hours=24)
CAPABILITY_DEFAULT_RATE_BUDGET = 120
CAPABILITY_DEFAULT_RATE_WINDOW = timedelta(minutes=1)
CAPABILITY_DEFAULT_WAKE_COOLDOWN = timedelta(seconds=30)
CAPABILITY_MAX_RATE_WINDOW = timedelta(hours=1)
CAPABILITY_MAX_WAKE_COOLDOWN = timedelta(hours=1)
CAPABILITY_START_PROFILE = ProfileId.MINECRAFT_SUNLIT_COBBLEMON


class CapabilityAudience(StrEnum):
    LAZYMC = "lazymc"
    HELIOS_MCP = "helios-mcp"


class CapabilityRole(StrEnum):
    OBSERVER = "observer"
    WAKER = "waker"


class CapabilityScope(StrEnum):
    STATUS = "status"
    WAKE = "wake"
    TPS = "tps"


class CapabilityError(Exception):
    """Safe internal error with a stable HTTP-facing code."""

    def __init__(self, code: str, message: str, *, status: int = 403, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


class CapabilityAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["status", "wake", "tps"]
    window: Literal["1h", "6h", "24h"] = "6h"

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    action: CapabilityAction

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, repr=False)
class IssuedCapability:
    token_id: str
    token: str
    role: CapabilityRole
    audience: CapabilityAudience
    scopes: frozenset[CapabilityScope]
    profile_id: ProfileId | None
    expires_at: datetime
    rate_budget: int

    def __repr__(self) -> str:
        return (
            "IssuedCapability("
            f"token_id={self.token_id!r}, role={self.role.value!r}, "
            f"audience={self.audience.value!r}, scopes={sorted(s.value for s in self.scopes)!r}, "
            f"profile_id={self.profile_id.value if self.profile_id else None!r}, "
            f"expires_at={self.expires_at.isoformat()!r}, rate_budget={self.rate_budget}, "
            "token='[show-once]')"
        )


@dataclass(frozen=True)
class _Grant:
    token_id: str
    role: CapabilityRole
    audience: CapabilityAudience
    scopes: frozenset[CapabilityScope]
    profile_id: ProfileId | None
    expires_at: datetime
    rate_budget: int
    rate_window_seconds: int
    wake_cooldown_seconds: int


@dataclass(frozen=True)
class CapabilityHttpResult:
    status: int
    body: dict[str, Any]


class CapabilityTokenStore:
    """Hash-only token store with bounded replay and audit records."""

    def __init__(self, db: sqlite3.Connection, *, clock: Callable[[], datetime] | None = None):
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS capability_tokens (
                token_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('observer', 'waker')),
                audience TEXT NOT NULL CHECK (audience IN ('helios-mcp', 'lazymc')),
                scopes TEXT NOT NULL,
                profile_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                rate_budget INTEGER NOT NULL CHECK (rate_budget BETWEEN 1 AND 1000),
                rate_window_seconds INTEGER NOT NULL DEFAULT 60 CHECK (rate_window_seconds BETWEEN 1 AND 3600),
                rate_window_started_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z',
                rate_window_used_count INTEGER NOT NULL DEFAULT 0 CHECK (rate_window_used_count >= 0),
                wake_cooldown_seconds INTEGER NOT NULL DEFAULT 30 CHECK (wake_cooldown_seconds BETWEEN 0 AND 3600),
                last_wake_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS capability_requests (
                token_id TEXT NOT NULL REFERENCES capability_tokens(token_id),
                request_id TEXT NOT NULL,
                canonical_hash TEXT NOT NULL,
                response_json TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed')),
                created_at TEXT NOT NULL,
                PRIMARY KEY (token_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS capability_audit (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                token_id TEXT,
                audience TEXT,
                action TEXT NOT NULL,
                result TEXT NOT NULL CHECK (result IN ('issued', 'accepted', 'succeeded', 'failed', 'rejected', 'revoked')),
                detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_capability_tokens_hash ON capability_tokens(token_hash);
            CREATE INDEX IF NOT EXISTS idx_capability_requests_created ON capability_requests(created_at);
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(capability_tokens)")}
        migrations = {
            "rate_window_seconds": "INTEGER NOT NULL DEFAULT 60",
            "rate_window_started_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z'",
            "rate_window_used_count": "INTEGER NOT NULL DEFAULT 0",
            "wake_cooldown_seconds": "INTEGER NOT NULL DEFAULT 30",
            "last_wake_at": "TEXT",
        }
        for name, definition in migrations.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE capability_tokens ADD COLUMN {name} {definition}")
        self.db.commit()

    @staticmethod
    def _iso(value: datetime) -> str:
        current = value if value.tzinfo and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(b"helios-horizon-capability-v1\x00" + token.encode("utf-8")).hexdigest()

    @staticmethod
    def _scopes(role: CapabilityRole) -> frozenset[CapabilityScope]:
        return (
            frozenset({CapabilityScope.STATUS, CapabilityScope.TPS})
            if role is CapabilityRole.OBSERVER
            else frozenset({CapabilityScope.STATUS, CapabilityScope.WAKE})
        )

    @staticmethod
    def _validate_binding(
        role: CapabilityRole, audience: CapabilityAudience, profile_id: ProfileId | None
    ) -> None:
        if role is CapabilityRole.OBSERVER:
            if audience is not CapabilityAudience.HELIOS_MCP or profile_id is not None:
                raise CapabilityError("invalid_binding", "capability binding is not approved", status=422)
            return
        if (
            role is not CapabilityRole.WAKER
            or audience not in {CapabilityAudience.LAZYMC, CapabilityAudience.HELIOS_MCP}
            or profile_id is not CAPABILITY_START_PROFILE
        ):
            raise CapabilityError("invalid_binding", "capability binding is not approved", status=422)

    def _audit(
        self,
        action: str,
        result: str,
        *,
        token_id: str | None = None,
        audience: CapabilityAudience | None = None,
        detail: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO capability_audit(id,timestamp,token_id,audience,action,result,detail) VALUES(?,?,?,?,?,?,?)",
            (
                secrets.token_hex(16),
                self._iso(self.clock()),
                token_id,
                audience.value if audience is not None else None,
                str(action)[:64],
                str(result)[:16],
                str(detail)[:256],
            ),
        )

    def issue(
        self,
        *,
        role: CapabilityRole,
        audience: CapabilityAudience,
        profile_id: ProfileId | None = None,
        ttl: timedelta = CAPABILITY_DEFAULT_TTL,
        rate_budget: int = CAPABILITY_DEFAULT_RATE_BUDGET,
        rate_window: timedelta = CAPABILITY_DEFAULT_RATE_WINDOW,
        wake_cooldown: timedelta = CAPABILITY_DEFAULT_WAKE_COOLDOWN,
    ) -> IssuedCapability:
        try:
            role = CapabilityRole(role)
            audience = CapabilityAudience(audience)
        except ValueError as exc:
            raise CapabilityError("invalid_binding", "capability binding is not approved", status=422) from exc
        if profile_id is not None:
            try:
                profile_id = ProfileId(profile_id)
            except ValueError as exc:
                raise CapabilityError("invalid_binding", "capability binding is not approved", status=422) from exc
        self._validate_binding(role, audience, profile_id)
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0) or ttl > CAPABILITY_MAX_TTL:
            raise CapabilityError("invalid_expiry", "capability expiry is outside the approved bound", status=422)
        if isinstance(rate_budget, bool) or not isinstance(rate_budget, int) or not 1 <= rate_budget <= 1000:
            raise CapabilityError("invalid_rate_budget", "capability rate budget is outside the approved bound", status=422)
        if (
            not isinstance(rate_window, timedelta)
            or rate_window <= timedelta(0)
            or rate_window > CAPABILITY_MAX_RATE_WINDOW
        ):
            raise CapabilityError("invalid_rate_window", "capability rate window is outside the approved bound", status=422)
        if (
            not isinstance(wake_cooldown, timedelta)
            or wake_cooldown < timedelta(0)
            or wake_cooldown > CAPABILITY_MAX_WAKE_COOLDOWN
        ):
            raise CapabilityError("invalid_wake_cooldown", "capability wake cooldown is outside the approved bound", status=422)
        rate_window_seconds = int(rate_window.total_seconds())
        wake_cooldown_seconds = int(wake_cooldown.total_seconds())
        if rate_window_seconds < 1 or wake_cooldown_seconds < 0:
            raise CapabilityError("invalid_rate_window", "capability rate window is outside the approved bound", status=422)
        now = self.clock().astimezone(timezone.utc)
        expires_at = now + ttl
        token_id = secrets.token_hex(16)
        token = "hc_" + secrets.token_urlsafe(32)
        scopes = self._scopes(role)
        self.db.execute(
            "INSERT INTO capability_tokens(token_id,token_hash,role,audience,scopes,profile_id,created_at,expires_at,rate_budget,rate_window_seconds,rate_window_started_at,wake_cooldown_seconds) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                token_id,
                self._hash(token),
                role.value,
                audience.value,
                json.dumps(sorted(scope.value for scope in scopes)),
                profile_id.value if profile_id is not None else None,
                self._iso(now),
                self._iso(expires_at),
                rate_budget,
                rate_window_seconds,
                self._iso(now),
                wake_cooldown_seconds,
            ),
        )
        self._audit("issue", "issued", token_id=token_id, audience=audience, detail=f"role={role.value}")
        self.db.commit()
        return IssuedCapability(token_id, token, role, audience, scopes, profile_id, expires_at, rate_budget)

    def revoke(self, token_id: str) -> None:
        if not isinstance(token_id, str) or not token_id or len(token_id) > 64:
            raise CapabilityError("invalid_token_id", "token id is invalid", status=422)
        now = self._iso(self.clock())
        row = self.db.execute("SELECT audience FROM capability_tokens WHERE token_id=?", (token_id,)).fetchone()
        if row is None:
            raise CapabilityError("unknown_token", "capability token is not available", status=404)
        self.db.execute("UPDATE capability_tokens SET revoked_at=? WHERE token_id=? AND revoked_at IS NULL", (now, token_id))
        self._audit("revoke", "revoked", token_id=token_id, detail="token revoked")
        self.db.commit()

    def _grant(self, token: str, expected_audience: CapabilityAudience) -> _Grant:
        if not isinstance(token, str) or not 40 <= len(token) <= 128 or "\n" in token or "\r" in token:
            raise CapabilityError("invalid_token", "capability token is invalid", status=401)
        try:
            expected_audience = CapabilityAudience(expected_audience)
        except ValueError as exc:
            raise CapabilityError("invalid_audience", "capability audience is invalid", status=403) from exc
        row = self.db.execute(
            "SELECT token_id,role,audience,scopes,profile_id,expires_at,rate_budget,rate_window_seconds,wake_cooldown_seconds,revoked_at "
            "FROM capability_tokens WHERE token_hash=?",
            (self._hash(token),),
        ).fetchone()
        if row is None:
            raise CapabilityError("invalid_token", "capability token is invalid", status=401)
        token_id, role, audience, scopes_json, profile_id, expires_at, rate_budget, rate_window_seconds, wake_cooldown_seconds, revoked_at = row
        try:
            role_enum = CapabilityRole(role)
            audience_enum = CapabilityAudience(audience)
            scopes = frozenset(CapabilityScope(value) for value in json.loads(scopes_json))
            bound_profile = ProfileId(profile_id) if profile_id else None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CapabilityError("invalid_token", "capability token is invalid", status=401) from exc
        if audience_enum is not expected_audience:
            raise CapabilityError("audience_mismatch", "capability audience is not permitted", status=403)
        if revoked_at is not None:
            raise CapabilityError("token_revoked", "capability token is revoked", status=401)
        if self._parse(expires_at) <= self.clock().astimezone(timezone.utc):
            raise CapabilityError("token_expired", "capability token is expired", status=401)
        try:
            self._validate_binding(role_enum, audience_enum, bound_profile)
        except CapabilityError as exc:
            raise CapabilityError("invalid_token", "capability token is invalid", status=401) from exc
        return _Grant(
            token_id,
            role_enum,
            audience_enum,
            scopes,
            bound_profile,
            self._parse(expires_at),
            int(rate_budget),
            int(rate_window_seconds),
            int(wake_cooldown_seconds),
        )

    def _consume_budget(self, grant: _Grant, kind: str) -> None:
        now = self.clock().astimezone(timezone.utc)
        now_iso = self._iso(now)
        if kind == "wake":
            cooldown_cutoff = self._iso(now - timedelta(seconds=grant.wake_cooldown_seconds))
            cursor = self.db.execute(
                "UPDATE capability_tokens SET last_wake_at=? "
                "WHERE token_id=? AND revoked_at IS NULL AND expires_at>? "
                "AND (last_wake_at IS NULL OR last_wake_at<=?)",
                (now_iso, grant.token_id, now_iso, cooldown_cutoff),
            )
            if cursor.rowcount != 1:
                self._audit("wake", "rejected", token_id=grant.token_id, audience=grant.audience, detail="wake cooldown active")
                self.db.commit()
                raise CapabilityError("wake_cooldown", "capability wake cooldown is active", status=429, retryable=True)
            return

        window_cutoff = self._iso(now - timedelta(seconds=grant.rate_window_seconds))
        cursor = self.db.execute(
            "UPDATE capability_tokens SET "
            "rate_window_started_at=CASE WHEN rate_window_started_at<=? THEN ? ELSE rate_window_started_at END, "
            "rate_window_used_count=CASE WHEN rate_window_started_at<=? THEN 1 ELSE rate_window_used_count+1 END "
            "WHERE token_id=? AND revoked_at IS NULL AND expires_at>? "
            "AND (rate_window_started_at<=? OR rate_window_used_count<rate_budget)",
            (window_cutoff, now_iso, window_cutoff, grant.token_id, now_iso, window_cutoff),
        )
        if cursor.rowcount != 1:
            self._audit("request", "rejected", token_id=grant.token_id, audience=grant.audience, detail="rate window exhausted")
            self.db.commit()
            raise CapabilityError("rate_window_exhausted", "capability rate window exhausted", status=429, retryable=True)

    def begin(self, token: str, audience: CapabilityAudience, request: CapabilityRequest) -> tuple[_Grant, dict[str, Any] | None]:
        grant = self._grant(token, audience)
        canonical_hash = hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()
        request_id = str(request.request_id)
        row = self.db.execute(
            "SELECT canonical_hash,response_json,status FROM capability_requests WHERE token_id=? AND request_id=?",
            (grant.token_id, request_id),
        ).fetchone()
        if row is not None:
            if not hmac.compare_digest(str(row[0]), canonical_hash):
                self._audit("request", "rejected", token_id=grant.token_id, audience=grant.audience, detail="request id conflict")
                self.db.commit()
                raise CapabilityError("request_id_conflict", "request id was already used", status=409)
            if row[2] == "completed" and row[1]:
                try:
                    return grant, json.loads(row[1])
                except json.JSONDecodeError as exc:
                    raise CapabilityError("internal_error", "stored capability response is unavailable", status=503, retryable=True) from exc
            raise CapabilityError("request_in_progress", "request is still in progress", status=409, retryable=True)
        self._consume_budget(grant, request.action.kind)
        self.db.execute(
            "INSERT INTO capability_requests(token_id,request_id,canonical_hash,status,created_at) VALUES(?,?,?,?,?)",
            (grant.token_id, request_id, canonical_hash, "pending", self._iso(self.clock())),
        )
        self._audit("request", "accepted", token_id=grant.token_id, audience=grant.audience, detail=f"kind={request.action.kind}")
        self.db.commit()
        return grant, None

    def complete(self, grant: _Grant, request: CapabilityRequest, response: dict[str, Any], *, ok: bool) -> None:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) > CAPABILITY_MAX_RESPONSE_BYTES:
            response = {"error": {"code": "response_too_large", "message": "capability response exceeded its bound", "retryable": False}}
            encoded = json.dumps(response, separators=(",", ":"))
            ok = False
        self.db.execute(
            "UPDATE capability_requests SET response_json=?,status='completed' WHERE token_id=? AND request_id=? AND status='pending'",
            (encoded, grant.token_id, str(request.request_id)),
        )
        self._audit(
            request.action.kind,
            "succeeded" if ok else "failed",
            token_id=grant.token_id,
            audience=grant.audience,
            detail="completed" if ok else "safe failure",
        )
        self.db.commit()


class CapabilityService:
    """Map capability actions onto the existing typed controller only."""

    def __init__(self, store: CapabilityTokenStore, rpc: Callable[..., Any]):
        self.store = store
        self.rpc = rpc

    @staticmethod
    def parse_request(payload: object) -> CapabilityRequest:
        if not isinstance(payload, dict):
            raise CapabilityError("invalid_request", "capability request is invalid", status=422)
        try:
            return CapabilityRequest.model_validate(payload)
        except ValidationError as exc:
            raise CapabilityError("invalid_request", "capability request is invalid", status=422) from exc

    async def _call(self, request: RpcRequest) -> RpcResponse:
        try:
            parameters = inspect.signature(self.rpc).parameters
            value = self.rpc(request) if len(parameters) < 2 else self.rpc(request.actor, request.action)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, (RpcSuccess, RpcFailure)):
                return value
            return RpcSuccess(request_id=request.request_id, result=value)
        except Exception as exc:
            raise CapabilityError("upstream_unavailable", "control service unavailable", status=503, retryable=True) from exc

    @staticmethod
    def _status_snapshot(response: RpcResponse) -> StatusSnapshot | None:
        if not isinstance(response, RpcSuccess):
            return None
        if isinstance(response.result, StatusSnapshot):
            return response.result
        if not isinstance(response.result, dict):
            return None
        try:
            return StatusSnapshot.model_validate(response.result)
        except ValidationError:
            return None

    @classmethod
    def _active_wake_result(cls, response: RpcResponse) -> dict[str, Any] | None:
        snapshot = cls._status_snapshot(response)
        if snapshot is None:
            return None
        target = next(
            (item for item in snapshot.profiles if item.profile_id is CAPABILITY_START_PROFILE),
            None,
        )
        if (
            target is not None
            and target.slot_owner is CAPABILITY_START_PROFILE
            and target.state in {ObservedState.STARTING, ObservedState.RUNNING}
        ):
            return {"state": target.state.value, "already_active": True}
        return None

    @staticmethod
    def _failure_result(response: RpcFailure) -> dict[str, Any]:
        return {
            "error": {
                "code": response.error.code.value,
                "message": response.error.message,
                "retryable": response.error.retryable,
                "details": response.error.details.model_dump(mode="json") if response.error.details else None,
            }
        }

    async def _dispatch(self, grant: _Grant, request: CapabilityRequest) -> dict[str, Any]:
        kind = request.action.kind
        if kind == "tps" and CapabilityScope.TPS not in grant.scopes:
            raise CapabilityError("scope_denied", "capability scope is not permitted", status=403)
        if kind == "wake" and CapabilityScope.WAKE not in grant.scopes:
            raise CapabilityError("scope_denied", "capability scope is not permitted", status=403)
        if kind == "status" and CapabilityScope.STATUS not in grant.scopes:
            raise CapabilityError("scope_denied", "capability scope is not permitted", status=403)
        if grant.role is CapabilityRole.WAKER and kind not in {"status", "wake"}:
            raise CapabilityError("scope_denied", "waker capability is limited to status and wake", status=403)
        actor = f"capability-{grant.role.value}"
        if kind == "status":
            action: Any = GetStatus(kind="get_status", refresh=True)
        elif kind == "wake":
            preflight = await self._call(
                RpcRequest(
                    request_id=uuid4(),
                    actor=actor,
                    action=GetStatus(kind="get_status", refresh=True),
                )
            )
            active = self._active_wake_result(preflight)
            if active is not None:
                return active
            if isinstance(preflight, RpcFailure):
                return self._failure_result(preflight)
            snapshot = self._status_snapshot(preflight)
            if snapshot is None:
                raise CapabilityError(
                    "upstream_unavailable",
                    "control service status is unavailable",
                    status=503,
                    retryable=True,
                )
            target = next(
                (item for item in snapshot.profiles if item.profile_id is CAPABILITY_START_PROFILE),
                None,
            )
            if target is None:
                raise CapabilityError(
                    "upstream_unavailable",
                    "control service status is unavailable",
                    status=503,
                    retryable=True,
                )
            if target.slot_owner is not None:
                raise CapabilityError("slot_conflict", "game slot is reserved", status=409)
            action = Start(kind="start", profile_id=CAPABILITY_START_PROFILE)
        else:
            action = GetStatsTps(
                kind="get_stats_tps",
                profile_id=CAPABILITY_START_PROFILE,
                window=request.action.window,
            )
        response = await self._call(
            RpcRequest(
                request_id=request.request_id,
                actor=actor,
                action=action,
            )
        )
        if kind == "wake" and isinstance(response, RpcFailure) and response.error.code is ErrorCode.SLOT_CONFLICT:
            status_response = await self._call(
                RpcRequest(
                    request_id=uuid4(),
                    actor=actor,
                    action=GetStatus(kind="get_status", refresh=True),
                )
            )
            active = self._active_wake_result(status_response)
            if active is not None:
                return active
        if isinstance(response, RpcFailure):
            return self._failure_result(response)
        result = response.result.model_dump(mode="json") if hasattr(response.result, "model_dump") else response.result
        status_snapshot = self._status_snapshot(response)
        if grant.role is CapabilityRole.WAKER and kind == "status":
            if status_snapshot is None:
                raise CapabilityError(
                    "upstream_unavailable",
                    "control service status is unavailable",
                    status=503,
                    retryable=True,
                )
            result = status_snapshot.model_copy(
                update={
                    "profiles": tuple(
                        item for item in status_snapshot.profiles if item.profile_id is CAPABILITY_START_PROFILE
                    )
                }
            ).model_dump(mode="json")
        if isinstance(result, dict):
            return result
        if isinstance(result, (list, tuple)):
            return {"items": list(result)}
        if isinstance(result, JobAccepted):
            return result.model_dump(mode="json")
        return {"result": result}

    async def handle(self, token: str, audience: CapabilityAudience, request: CapabilityRequest) -> CapabilityHttpResult:
        try:
            grant, replay = self.store.begin(token, audience, request)
            if replay is not None:
                return CapabilityHttpResult(200, replay)
            try:
                body = await self._dispatch(grant, request)
                ok = "error" not in body
                status = 200 if ok else (503 if body["error"].get("retryable") else 400)
            except CapabilityError as exc:
                body = {"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}
                status = exc.status
                ok = False
            self.store.complete(grant, request, body, ok=ok)
            return CapabilityHttpResult(status, body)
        except CapabilityError as exc:
            return CapabilityHttpResult(exc.status, {"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}})


__all__ = [
    "CAPABILITY_DEFAULT_RATE_BUDGET",
    "CAPABILITY_DEFAULT_RATE_WINDOW",
    "CAPABILITY_DEFAULT_TTL",
    "CAPABILITY_DEFAULT_WAKE_COOLDOWN",
    "CAPABILITY_PATH_PREFIX",
    "CAPABILITY_START_PROFILE",
    "CapabilityAction",
    "CapabilityAudience",
    "CapabilityError",
    "CapabilityHttpResult",
    "CapabilityRequest",
    "CapabilityRole",
    "CapabilityScope",
    "CapabilityService",
    "CapabilityTokenStore",
    "IssuedCapability",
]
