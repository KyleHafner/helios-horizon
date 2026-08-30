"""FastAPI entrypoint for the proxy-authenticated game-control web process."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Mapping
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .api import ApiService, add_api_routes
from .db_telemetry import collect_database_telemetry_async
from .capability import (
    CAPABILITY_MAX_BODY_BYTES,
    CAPABILITY_PATH_PREFIX,
    CapabilityAudience,
    CapabilityError,
    CapabilityService,
    CapabilityTokenStore,
)
from .auth import (
    PROXY_CREDENTIAL_PATH,
    SESSION_COOKIE,
    SessionStore,
    authenticate_proxy,
    load_proxy_credential,
    set_session_cookie,
    validate_origin,
)
from .protocol import (
    CheckUpdate,
    GetPerf,
    GetLogs,
    GetNotificationConfig,
    GetProfileConfig,
    GetSchedules,
    GetProfiles,
    GetStatus,
    Watch,
    ListAudit,
    ListBackups,
    ListEvents,
    RpcFailure,
    RpcProvenance,
    RpcRequest,
    RpcResponse,
    RpcSuccess,
    response_from_json,
    MAX_RESPONSE_BYTES,
)
from .redaction import Redactor

CONTROL_SOCKET = Path("/run/game-control/control.sock")
PRODUCTION_WEB_ROOT = Path("/opt/game-control/web")
PUBLIC_ORIGIN = os.environ.get("HORIZON_PUBLIC_ORIGIN", "https://games.example.com")
_READ_ACTIONS = (
    GetStatus,
    GetPerf,
    GetProfiles,
    GetLogs,
    ListBackups,
    ListEvents,
    ListAudit,
    CheckUpdate,
    GetNotificationConfig,
    GetProfileConfig,
    GetSchedules,
)
_OPERATION_TIMEOUT_SECONDS = 300.0
SSE_RETRY_HINT = b"retry: 3000\n\n"


class BoundedTimingRing:
    """A bounded, allocation-light timing sample ring."""

    def __init__(self, *, maxlen: int = 1024):
        if not 1 <= maxlen <= 65536:
            raise ValueError("timing ring bound out of range")
        self._samples: deque[float] = deque(maxlen=maxlen)

    def record(self, duration_ms: float) -> None:
        self._samples.append(max(0.0, float(duration_ms)))

    def snapshot(self) -> dict[str, float | int | None]:
        values = sorted(self._samples)
        if not values:
            return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        return {
            "count": len(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "max_ms": values[-1],
        }


def _percentile(values: list[float], fraction: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


class WebPerformance:
    def __init__(self):
        self.routes: dict[str, BoundedTimingRing] = {}
        self.rpc = BoundedTimingRing()
        self.client = {
            "stats_fetch": BoundedTimingRing(maxlen=512),
            "recorder_draw": BoundedTimingRing(maxlen=512),
            "first_status_paint": BoundedTimingRing(maxlen=512),
        }
        self._client_batches: deque[float] = deque(maxlen=60)

    def record_route(self, route: str, duration_ms: float) -> None:
        self.routes.setdefault(route, BoundedTimingRing()).record(duration_ms)

    def snapshot(self, hub: "EventHub") -> dict[str, Any]:
        result = {route: ring.snapshot() for route, ring in self.routes.items()}
        result["rpc"] = self.rpc.snapshot()
        result["sse"] = hub.perf_snapshot()
        result["client"] = {name: ring.snapshot() for name, ring in self.client.items()}
        return result

    def record_client(self, samples: tuple["ClientPerformanceSample", ...], *, now: float) -> None:
        while self._client_batches and now - self._client_batches[0] >= 60.0:
            self._client_batches.popleft()
        if len(self._client_batches) >= 60:
            raise HTTPException(429, "client performance summary rate exceeded")
        self._client_batches.append(now)
        for sample in samples:
            self.client[sample.metric].record(sample.duration_ms)


class ClientPerformanceSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric: Literal["stats_fetch", "recorder_draw", "first_status_paint"]
    duration_ms: float = Field(ge=0, le=30_000)


class ClientPerformanceBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    samples: tuple[ClientPerformanceSample, ...] = Field(min_length=1, max_length=16)


class _TimedApiService:
    def __init__(self, service: ApiService, timings: BoundedTimingRing):
        self.service = service
        self.timings = timings

    async def call(
        self,
        actor: str,
        action: Any,
        *,
        provenance: RpcProvenance = RpcProvenance.SERVICE,
        request_id: UUID | None = None,
    ) -> RpcResponse:
        started = time.monotonic()
        try:
            return await self.service.call(actor, action, provenance=provenance, request_id=request_id)
        finally:
            self.timings.record((time.monotonic() - started) * 1000.0)


class UnixRpcClient:
    """Typed newline-delimited Unix RPC client; no operational file access."""

    def __init__(self, socket_path: Path = CONTROL_SOCKET, *, timeout: float = 5.0):
        if Path(socket_path) != CONTROL_SOCKET:
            raise PermissionError("RPC socket path is not approved")
        self.socket_path = CONTROL_SOCKET
        self.timeout = timeout

    async def __call__(self, request: RpcRequest) -> RpcResponse:
        timeout = self.timeout if isinstance(request.action, _READ_ACTIONS) else _OPERATION_TIMEOUT_SECONDS
        try:
            connection = asyncio.open_unix_connection(self.socket_path, limit=MAX_RESPONSE_BYTES + 1)
        except TypeError:
            # Keep narrow test/compatibility transports that predate the
            # explicit limit working; production asyncio accepts `limit`.
            connection = asyncio.open_unix_connection(self.socket_path)
        reader, writer = await asyncio.wait_for(connection, timeout)
        try:
            writer.write(request.model_dump_json().encode() + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout)
            except TimeoutError:
                # Python 3.11 can translate a cancellation racing with
                # wait_for() completion into TimeoutError. Preserve the
                # caller's cancellation contract instead of reporting a
                # spurious RPC timeout.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                raise
            if not line:
                raise RuntimeError("RPC unavailable")
            if len(line) > MAX_RESPONSE_BYTES:
                raise RuntimeError("RPC response exceeded framing budget")
            return response_from_json(line)
        finally:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except asyncio.CancelledError:
                raise
            except OSError:
                pass


class UnixWatchClient:
    """Persistent typed watch connection with bounded reconnect fallback."""

    def __init__(self, socket_path: Path = CONTROL_SOCKET, *, timeout: float = 5.0):
        if Path(socket_path) != CONTROL_SOCKET:
            raise PermissionError("RPC socket path is not approved")
        self.socket_path = CONTROL_SOCKET
        self.timeout = timeout

    async def events(self, *, cursor: int = 0, generation: int = 0) -> AsyncIterator[dict[str, Any]]:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.socket_path), self.timeout)
        request = RpcRequest(
            request_id=uuid4(), actor="game-control-web",
            provenance=RpcProvenance.SERVICE,
            action=Watch(kind="watch", cursor=cursor, generation=generation),
        )
        try:
            writer.write(request.model_dump_json().encode() + b"\n")
            await asyncio.wait_for(writer.drain(), self.timeout)
            while True:
                line = await asyncio.wait_for(reader.readline(), 20.0)
                if not line:
                    raise RuntimeError("watch connection closed")
                frame = json.loads(line)
                if not isinstance(frame, dict) or set(frame) - {"sequence", "generation", "kind", "full", "payload"}:
                    raise RuntimeError("invalid watch frame")
                if not isinstance(frame.get("payload"), dict):
                    raise RuntimeError("invalid watch payload")
                yield frame
        finally:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except (asyncio.CancelledError, OSError):
                pass


@dataclass(eq=False)
class StreamClient:
    queue: asyncio.Queue[dict[str, Any]]
    disconnected: bool = False


class EventHub:
    """Bounded fan-out queue with monotonic IDs and replay history."""

    def __init__(self, *, max_queue: int = 32, max_history: int = 256, redactor: Redactor | None = None):
        if max_queue < 1 or max_queue > 1024:
            raise ValueError("queue bound out of range")
        self.max_queue = max_queue
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._clients: set[StreamClient] = set()
        self._next_id = 0
        self._publish_times: dict[int, float] = {}
        self._cached_status: dict[str, Any] | None = None
        self._flush_lags = BoundedTimingRing()
        self._lock = asyncio.Lock()
        self.redactor = redactor or Redactor()

    async def subscribe(self, *, last_event_id: int = 0) -> StreamClient:
        client = StreamClient(asyncio.Queue(maxsize=self.max_queue))
        async with self._lock:
            self._clients.add(client)
            replay = [
                event for event in self._history
                if event["id"] > last_event_id and event["event"] == "status"
            ]
            initial = replay[-1] if replay else None
            cached = self._cached_status
            if cached is not None and cached["id"] > last_event_id and (initial is None or cached["id"] >= initial["id"]):
                initial = cached
            if initial is not None:
                client.queue.put_nowait(initial)
        return client

    async def cache_status(self, data: dict[str, Any]) -> None:
        """Refresh the authoritative payload without minting a stream event ID."""
        safe = self._sanitize(data)
        async with self._lock:
            if self._cached_status is not None:
                self._cached_status = {
                    "id": self._cached_status["id"],
                    "event": "status",
                    "data": safe,
                }

    async def clear_cached_status(self) -> None:
        """Invalidate cached state after the publisher loses authority."""
        async with self._lock:
            self._cached_status = None
            # Status history is not replayable without a current authoritative
            # snapshot; retain non-status audit/event frames and monotonic IDs.
            self._history = deque(
                (event for event in self._history if event["event"] != "status"),
                maxlen=self._history.maxlen,
            )

    async def unsubscribe(self, client: StreamClient) -> None:
        async with self._lock:
            client.disconnected = True
            self._clients.discard(client)

    async def send_to(self, client: StreamClient, event: str, data: dict[str, Any]) -> dict[str, Any]:
        """Queue a one-client event without adding it to replay history."""
        safe = self._sanitize(data)
        async with self._lock:
            self._next_id += 1
            item = {"id": self._next_id, "event": str(event)[:64], "data": safe}
            if not client.disconnected:
                try:
                    client.queue.put_nowait(item)
                except asyncio.QueueFull:
                    client.queue.get_nowait()
                    client.queue.put_nowait(item)
            return item

    def _sanitize(self, data: dict[str, Any]) -> dict[str, Any]:
        safe = {key: value for key, value in data.items() if key not in {"html", "dom", "view_reset", "command", "path", "url"}}
        if "message" in safe:
            safe["message"] = self.redactor.redact(str(safe["message"]))[:8192]
            if any(marker in safe["message"].casefold() for marker in ("view-reset", "innerhtml", "document.")):
                safe["message"] = "[REDACTED]"
        if isinstance(safe.get("logs"), list):
            safe["logs"] = self.redactor.redact_records(safe["logs"])[:500]
        return safe

    async def publish(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        safe = self._sanitize(data)
        async with self._lock:
            self._next_id += 1
            item = {"id": self._next_id, "event": str(event)[:64], "data": safe}
            self._history.append(item)
            if event == "status":
                self._cached_status = item
            self._publish_times[item["id"]] = time.monotonic()
            max_times = self._history.maxlen or 256
            while len(self._publish_times) > max_times:
                del self._publish_times[next(iter(self._publish_times))]
            for client in tuple(self._clients):
                if client.disconnected:
                    self._clients.discard(client)
                    continue
                try:
                    client.queue.put_nowait(item)
                except asyncio.QueueFull:
                    try:
                        client.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    if not client.disconnected:
                        client.queue.put_nowait(item)
            return item

    def record_flush(self, item: dict[str, Any]) -> None:
        published = self._publish_times.get(int(item.get("id", 0)))
        if published is not None:
            self._flush_lags.record((time.monotonic() - published) * 1000.0)

    def perf_snapshot(self) -> dict[str, Any]:
        return {
            "connected_clients": len(self._clients),
            "publish_flush_lag_ms": self._flush_lags.snapshot(),
        }


class AdaptiveStatusCadence:
    """Choose a cheap idle sampling interval while preserving fast transitions."""

    def __init__(self, *, fast_interval: float = 3.0, idle_interval: float = 15.0):
        self.fast_interval = max(0.1, float(fast_interval))
        self.idle_interval = max(self.fast_interval, float(idle_interval))
        self._previous_signature: tuple[Any, ...] | None = None

    def interval_for(self, snapshot: Any) -> float:
        profiles = snapshot.get("profiles", ()) if isinstance(snapshot, Mapping) else getattr(snapshot, "profiles", ())
        owner_id = None
        players = None
        jobs: list[Any] = []
        for profile in profiles or ():
            profile_id = self._value(self._field(profile, "profile_id"))
            slot_owner = self._value(self._field(profile, "slot_owner"))
            if slot_owner is not None and slot_owner == profile_id:
                owner_id = profile_id
                players = self._field(profile, "players_online")
            job = self._field(profile, "active_job_id")
            if job is not None:
                jobs.append(self._value(job))
        signature = (owner_id, players, tuple(jobs))
        transitioned = self._previous_signature is not None and signature != self._previous_signature
        self._previous_signature = signature
        idle = not jobs and (owner_id is None or players == 0)
        return self.fast_interval if transitioned or not idle else self.idle_interval

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)

    @staticmethod
    def _value(value: Any) -> Any:
        return getattr(value, "value", value)

def _sse_frame(item: dict[str, Any]) -> bytes:
    return (
        f"id: {int(item['id'])}\n"
        f"event: {str(item['event'])[:64]}\n"
        f"data: {json.dumps(item['data'], separators=(',', ':'), ensure_ascii=True)}\n\n"
    ).encode()


_MAX_SSE_EVENT_ID = 2**63 - 1


def _parse_sse_cursor(value: str | None) -> int | None:
    """Parse one bounded decimal cursor; invalid input fails closed."""
    if (value is None or len(value) > 19 or not value.isascii()
            or not (value == "0" or (value and value[0] != "0" and value.isdecimal()))):
        return None
    # Decimal-only input rejects signs, whitespace, separators, and exponents.
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= _MAX_SSE_EVENT_ID else None


def _last_event_id(request: Request) -> int:
    """Use a valid native cursor first, then the bounded replacement query cursor.

    A malformed present native header fails closed to zero.  Only an absent
    native header permits the valid replacement query cursor to be used.
    """
    if "last-event-id" in request.headers:
        return _parse_sse_cursor(request.headers.get("last-event-id")) or 0
    return _parse_sse_cursor(request.query_params.get("after")) or 0


def create_app(
    *,
    rpc: Callable[..., Any] | None = None,
    rpc_client: Callable[..., Any] | None = None,
    proxy_credential: str | None = None,
    session_db: sqlite3.Connection | str | None = None,
    allowed_origins: set[str] | frozenset[str] | None = None,
    hub: EventHub | None = None,
    web_root: Path | None = None,
    status_cadence: AdaptiveStatusCadence | None = None,
    capability_service: CapabilityService | None = None,
) -> FastAPI:
    """Build the app with explicit test seams and fixed production resources."""

    if session_db is None:
        sessions = SessionStore.open()
    elif isinstance(session_db, sqlite3.Connection):
        sessions = SessionStore(session_db)
    elif session_db == ":memory:":
        sessions = SessionStore(sqlite3.connect(":memory:", check_same_thread=False))
    else:
        raise PermissionError("session database path is not approved")
    rpc_impl = rpc_client or rpc or UnixRpcClient()
    stream_hub = hub or EventHub()
    web_performance = WebPerformance()
    service = _TimedApiService(ApiService(rpc_impl), web_performance.rpc)
    capabilities = capability_service or CapabilityService(CapabilityTokenStore(sessions.db), rpc_impl)
    mutation_wakeup = asyncio.Event()
    watch_connected = asyncio.Event()
    credential = proxy_credential
    assets = Path(web_root) if web_root is not None else PRODUCTION_WEB_ROOT
    origins = (
        frozenset(allowed_origins)
        if allowed_origins is not None
        else frozenset({PUBLIC_ORIGIN})
    )
    async def publish_status_loop() -> None:
        last_snapshot: dict[str, Any] | None = None
        last_published = 0.0
        cadence = status_cadence or AdaptiveStatusCadence()
        forced_fast_cycles = 0
        consecutive_failures = 0
        while True:
            interval = cadence.fast_interval
            try:
                if watch_connected.is_set():
                    # The persistent watch owns authoritative updates while
                    # connected.  Keep the connect-per-call path dormant so a
                    # reconnect cannot duplicate snapshots or create polling
                    # traffic in the browser-facing path.
                    try:
                        await asyncio.wait_for(mutation_wakeup.wait(), timeout=15.0)
                    except asyncio.TimeoutError:
                        pass
                    mutation_wakeup.clear()
                    continue
                result = await service.call("status-publisher", GetStatus(kind="get_status", refresh=True))
                if isinstance(result, RpcSuccess):
                    consecutive_failures = 0
                    if forced_fast_cycles:
                        forced_fast_cycles -= 1
                    snapshot = result.result.model_dump(mode="json") if hasattr(result.result, "model_dump") else result.result
                    await stream_hub.cache_status(snapshot)
                    comparable = dict(snapshot)
                    comparable.pop("observed_at", None)
                    now = asyncio.get_running_loop().time()
                    if comparable != last_snapshot or now - last_published >= 15:
                        last_snapshot = comparable
                        await stream_hub.publish("status", snapshot)
                        last_published = now
                    interval = cadence.interval_for(snapshot)
                    if forced_fast_cycles:
                        interval = cadence.fast_interval
                else:
                    consecutive_failures += 1
                    last_snapshot = None
                    await stream_hub.clear_cached_status()
                    interval = cadence.idle_interval if consecutive_failures >= 3 else cadence.fast_interval
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                last_snapshot = None
                await stream_hub.clear_cached_status()
                interval = cadence.idle_interval if consecutive_failures >= 3 else cadence.fast_interval
            try:
                await asyncio.wait_for(mutation_wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            mutation_wakeup.clear()
            forced_fast_cycles = max(forced_fast_cycles, 3)

    async def publish_watch_loop() -> None:
        """Prefer the persistent Unix watch; the status loop remains fallback."""
        cursor = 0
        generation = 0
        while True:
            try:
                async for frame in UnixWatchClient().events(cursor=cursor, generation=generation):
                    watch_connected.set()
                    sequence = int(frame.get("sequence", 0))
                    frame_generation = int(frame.get("generation", 0))
                    if sequence <= cursor or frame_generation < generation:
                        # Reconnect/replay may overlap the last delivered
                        # frame; never regress the authoritative projection.
                        continue
                    cursor = sequence
                    generation = frame_generation
                    kind = str(frame.get("kind", "event"))
                    if kind == "heartbeat":
                        continue
                    if kind == "full_resync":
                        # A marker carries no state. Obtain exactly one
                        # authoritative snapshot before exposing convergence
                        # to SSE clients; this is server-side and independent
                        # of browser visibility.
                        result = await service.call(
                            "watch-resync", GetStatus(kind="get_status", refresh=True)
                        )
                        if isinstance(result, RpcSuccess):
                            payload = result.result.model_dump(mode="json") if hasattr(result.result, "model_dump") else result.result
                            await stream_hub.publish("status", payload)
                        continue
                    await stream_hub.publish(kind, dict(frame["payload"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Connect-per-call status polling remains the bounded fallback;
                # retrying the watch is deliberately visibility-independent.
                watch_connected.clear()
                await asyncio.sleep(1.0)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        _app.state.status_publisher = asyncio.create_task(publish_status_loop())
        _app.state.watch_publisher = asyncio.create_task(publish_watch_loop())
        try:
            yield
        finally:
            tasks = [getattr(_app.state, name, None) for name in ("status_publisher", "watch_publisher")]
            for task in tasks:
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'",
        )
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
        # API responses contain session/control state and must not be cached;
        # SSE needs its own streaming cache policy and remains exempt here.
        if request.url.path.startswith("/api/") and request.url.path != "/api/v1/stream":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.middleware("http")
    async def request_timing(request: Request, call_next):
        started = time.monotonic()
        try:
            return await call_next(request)
        finally:
            route = request.scope.get("route")
            # Unmatched paths (scanners, typos) must not mint new ring keys.
            if route is not None:
                web_performance.record_route(
                    f"{request.method.upper()} {route.path}",
                    (time.monotonic() - started) * 1000.0,
                )

    async def auth_dependency(request: Request, response: Response, *, mutation: bool = False) -> str:
        try:
            actor = authenticate_proxy(request.headers, credential)
        except LookupError as exc:
            raise HTTPException(401, "identity required") from exc
        except PermissionError as exc:
            raise HTTPException(403, "forbidden") from exc
        session = request.cookies.get(SESSION_COOKIE)
        csrf = request.headers.get("x-csrf-token")
        if not session:
            if mutation:
                raise HTTPException(401, "session required")
            session, csrf_value = sessions.create(actor)
            set_session_cookie(response, session)
            # The CSRF value is not sent to a GET caller; clients obtain it
            # from the session bootstrap response.
            request.state.csrf_token = csrf_value
        elif mutation:
            if not sessions.validate(session, csrf, actor=actor):
                raise HTTPException(403, "csrf validation failed")
            origin = request.headers.get("origin")
            if not validate_origin(origin, origins):
                raise HTTPException(403, "origin validation failed")
        else:
            if not sessions.validate_session(session, actor=actor):
                raise HTTPException(401, "session required")
            # StreamingResponse does not preserve injected Response headers, so
            # cookie refresh on SSE is ineffective and needlessly writes SQLite.
            if request.url.path != "/api/v1/stream" and sessions.touch(session):
                set_session_cookie(response, session)
        # Keep terminal rows bounded without making cleanup a request-critical
        # operation. A small batch also works for long-lived deployments.
        try:
            sessions.prune()
        except (OSError, sqlite3.Error, ValueError):
            pass
        request.state.actor = actor
        # StreamingResponse cannot carry the cookie written to the injected
        # Response object.  Retain the exact authenticated token on the
        # request so a newly authenticated SSE stream can still perform the
        # same revocation checks as an established browser session.
        request.state.session_token = session
        return actor

    @app.get("/api/v1/session")
    async def session(request: Request, response: Response):
        actor = await auth_dependency(request, response)
        session_token = request.cookies.get(SESSION_COOKIE)
        current = sessions.get(session_token) if session_token else None
        csrf_token = getattr(request.state, "csrf_token", None)
        if csrf_token is None and session_token:
            try:
                csrf_token = sessions.rotate_csrf(session_token, actor=actor)
            except ValueError as exc:
                raise HTTPException(401, "session required") from exc
            current = sessions.get(session_token)
        return {"actor": actor, "csrf_token": csrf_token, "expires_at": current.expires_at.isoformat() if current else None}

    @app.post("/api/v1/session/revoke")
    async def revoke(request: Request, response: Response):
        await auth_dependency(request, response, mutation=True)
        session_token = request.cookies.get(SESSION_COOKIE)
        if session_token:
            sessions.revoke(session_token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    async def capability_request(request: Request, operation: str | None = None):
        """Handle the token-only external surface; no browser session is accepted."""
        try:
            # Do not trust Content-Length: proxies and chunked requests may
            # omit it or lie. Read the raw stream with a hard bound first.
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > CAPABILITY_MAX_BODY_BYTES:
                    raise CapabilityError(
                        "request_too_large",
                        "capability request body is too large",
                        status=413,
                    )
                raw.extend(chunk)
            if raw:
                try:
                    payload = json.loads(bytes(raw))
                except (UnicodeDecodeError, TypeError, ValueError) as exc:
                    raise CapabilityError("invalid_request", "invalid capability request", status=422) from exc
            else:
                payload = None
            capability_request_value = capabilities.parse_request(payload)
            if operation is not None and capability_request_value.action.kind != operation:
                raise CapabilityError("invalid_request", "capability operation does not match request", status=422)
            authorization = request.headers.get("authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not separator or not token:
                raise CapabilityError("invalid_token", "capability token is invalid", status=401)
            audience = CapabilityAudience(request.headers.get("x-horizon-capability-audience", ""))
        except ValueError as exc:
            raise HTTPException(403, "capability audience is not permitted") from exc
        except CapabilityError as exc:
            return JSONResponse(
                status_code=exc.status,
                content={"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}},
            )
        result = await capabilities.handle(token, audience, capability_request_value)
        return JSONResponse(status_code=result.status, content=result.body)

    async def capability_any(request: Request):
        return await capability_request(request, None)

    app.add_api_route(CAPABILITY_PATH_PREFIX, capability_any, methods=["POST"])

    def _typed_capability_route(operation: str):
        async def capability_typed(request: Request):
            return await capability_request(request, operation)

        return capability_typed

    for capability_operation in ("status", "wake", "tps"):
        app.add_api_route(
            f"{CAPABILITY_PATH_PREFIX}/{capability_operation}",
            _typed_capability_route(capability_operation),
            methods=["POST"],
        )

    router = __import__("fastapi").APIRouter(prefix="/api/v1")
    add_api_routes(router, service, auth_dependency, auth_dependency, on_mutation=mutation_wakeup.set)
    app.include_router(router)

    @app.get("/api/v1/stream")
    async def stream(request: Request, response: Response):
        actor = await auth_dependency(request, response)
        session_token = request.state.session_token
        client = await stream_hub.subscribe(last_event_id=_last_event_id(request))

        async def generate() -> AsyncIterator[bytes]:
            yield SSE_RETRY_HINT
            try:
                while not client.disconnected:
                    try:
                        item = await asyncio.wait_for(client.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # Re-check revocation at every heartbeat so an open
                        # SSE connection does not outlive logout/revocation.
                        if not sessions.validate_session(session_token, actor=actor):
                            break
                        yield b"event: heartbeat\ndata: {}\n\n"
                        if await request.is_disconnected():
                            break
                        continue
                    stream_hub.record_flush(item)
                    if not sessions.validate_session(session_token, actor=actor):
                        break
                    yield _sse_frame(item)
                    if await request.is_disconnected():
                        break
            finally:
                await stream_hub.unsubscribe(client)

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/perf")
    async def performance(request: Request, response: Response):
        actor = await auth_dependency(request, response)
        result = web_performance.snapshot(stream_hub)
        result["databases"] = {"web": await collect_database_telemetry_async(sessions, "web")}
        slotd = await service.call(actor, GetPerf(kind="get_perf"))
        if isinstance(slotd, RpcSuccess) and hasattr(slotd.result, "model_dump"):
            result["slotd"] = slotd.result.model_dump(mode="json")
        return result

    @app.post("/api/v1/perf/client")
    async def client_performance(request: Request, response: Response):
        await auth_dependency(request, response, mutation=True)
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(413, "client performance summary is too large")
        try:
            batch = ClientPerformanceBatch.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(422, "invalid client performance summary") from exc
        web_performance.record_client(batch.samples, now=time.monotonic())
        return {"accepted": len(batch.samples)}

    @app.get("/", response_class=FileResponse)
    async def index():
        return assets / "index.html"

    @app.get("/app.js", response_class=FileResponse)
    async def app_script():
        return assets / "app.js"

    @app.get("/commands.js", response_class=FileResponse)
    async def commands_script():
        return assets / "commands.js"

    @app.get("/palette.js", response_class=FileResponse)
    async def palette_script():
        return assets / "palette.js"

    @app.get("/styles.css", response_class=FileResponse)
    async def stylesheet():
        return assets / "styles.css"

    app.state.sessions = sessions
    app.state.rpc = rpc_impl
    app.state.event_hub = stream_hub
    app.state.proxy_credential_path = PROXY_CREDENTIAL_PATH
    app.state.capability_store = capabilities.store
    app.state.capability_service = capabilities
    return app


app = None

build_app = create_app


__all__ = [
    "CONTROL_SOCKET",
    "EventHub",
    "StreamClient",
    "UnixRpcClient",
    "UnixWatchClient",
    "create_app",
    "build_app",
]
