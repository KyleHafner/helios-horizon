"""FastAPI entrypoint for the proxy-authenticated game-control web process."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .api import ApiService, add_api_routes
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
    GetProfiles,
    GetStatus,
    ListAudit,
    ListBackups,
    ListEvents,
    RpcFailure,
    RpcProvenance,
    RpcRequest,
    RpcResponse,
    RpcSuccess,
    response_from_json,
)
from .redaction import Redactor

CONTROL_SOCKET = Path("/run/game-control/control.sock")
PRODUCTION_WEB_ROOT = Path("/opt/game-control/web")
PUBLIC_ORIGIN = "https://games.example.com"
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

    def record_route(self, route: str, duration_ms: float) -> None:
        self.routes.setdefault(route, BoundedTimingRing()).record(duration_ms)

    def snapshot(self, hub: "EventHub") -> dict[str, Any]:
        result = {route: ring.snapshot() for route, ring in self.routes.items()}
        result["rpc"] = self.rpc.snapshot()
        result["sse"] = hub.perf_snapshot()
        return result


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
    ) -> RpcResponse:
        started = time.monotonic()
        try:
            return await self.service.call(actor, action, provenance=provenance)
        finally:
            self.timings.record((time.monotonic() - started) * 1000.0)


async def _wait_for_preserving_cancellation(awaitable: Any, timeout: float) -> Any:
    """Apply an RPC timeout without converting caller cancellation to TimeoutError."""
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()
        raise


class UnixRpcClient:
    """Typed newline-delimited Unix RPC client; no operational file access."""

    def __init__(self, socket_path: Path = CONTROL_SOCKET, *, timeout: float = 5.0):
        if Path(socket_path) != CONTROL_SOCKET:
            raise PermissionError("RPC socket path is not approved")
        self.socket_path = CONTROL_SOCKET
        self.timeout = timeout

    async def __call__(self, request: RpcRequest) -> RpcResponse:
        timeout = self.timeout if isinstance(request.action, _READ_ACTIONS) else _OPERATION_TIMEOUT_SECONDS
        reader, writer = await _wait_for_preserving_cancellation(
            asyncio.open_unix_connection(self.socket_path), timeout
        )
        try:
            writer.write(request.model_dump_json().encode() + b"\n")
            await writer.drain()
            line = await _wait_for_preserving_cancellation(reader.readline(), timeout)
            if not line:
                raise RuntimeError("RPC unavailable")
            return response_from_json(line)
        finally:
            writer.close()
            try:
                await asyncio.shield(writer.wait_closed())
            except asyncio.CancelledError:
                raise
            except OSError:
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
            if replay:
                client.queue.put_nowait(replay[-1])
        return client

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


def _last_event_id(request: Request) -> int:
    value = request.headers.get("last-event-id", "0")
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(0, min(parsed, 2**63 - 1))


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
    mutation_wakeup = asyncio.Event()
    credential = proxy_credential
    assets = Path(web_root) if web_root is not None else PRODUCTION_WEB_ROOT
    origins = (
        frozenset(allowed_origins)
        if allowed_origins is not None
        else frozenset({PUBLIC_ORIGIN})
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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

    async def publish_status_loop() -> None:
        last_snapshot: dict[str, Any] | None = None
        last_published = 0.0
        cadence = status_cadence or AdaptiveStatusCadence()
        forced_fast_cycles = 0
        consecutive_failures = 0
        while True:
            interval = cadence.fast_interval
            try:
                result = await service.call("status-publisher", GetStatus(kind="get_status", refresh=True))
                if isinstance(result, RpcSuccess):
                    consecutive_failures = 0
                    if forced_fast_cycles:
                        forced_fast_cycles -= 1
                    snapshot = result.result.model_dump(mode="json") if hasattr(result.result, "model_dump") else result.result
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
                    interval = cadence.idle_interval if consecutive_failures >= 3 else cadence.fast_interval
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                interval = cadence.idle_interval if consecutive_failures >= 3 else cadence.fast_interval
            try:
                await asyncio.wait_for(mutation_wakeup.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            mutation_wakeup.clear()
            forced_fast_cycles = max(forced_fast_cycles, 3)

    @app.on_event("startup")
    async def start_status_publisher() -> None:
        app.state.status_publisher = asyncio.create_task(publish_status_loop())

    @app.on_event("shutdown")
    async def stop_status_publisher() -> None:
        task = getattr(app.state, "status_publisher", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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
            current = sessions.get(session)
            if current is None or current.actor != actor or current.expires_at <= datetime.now(timezone.utc):
                raise HTTPException(401, "session required")
            # StreamingResponse does not preserve injected Response headers, so
            # cookie refresh on SSE is ineffective and needlessly writes SQLite.
            if request.url.path != "/api/v1/stream" and sessions.touch(session):
                set_session_cookie(response, session)
        request.state.actor = actor
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

    router = __import__("fastapi").APIRouter(prefix="/api/v1")
    add_api_routes(router, service, auth_dependency, auth_dependency, on_mutation=mutation_wakeup.set)
    app.include_router(router)

    @app.get("/api/v1/stream")
    async def stream(request: Request, response: Response):
        actor = await auth_dependency(request, response)
        # The initial snapshot is an RPC read, just like REST status.  The
        # stream never opens a state database or game/log path.
        initial = await service.call(actor, GetStatus(kind="get_status"))
        client = await stream_hub.subscribe(last_event_id=_last_event_id(request))
        if isinstance(initial, RpcSuccess):
            snapshot = initial.result.model_dump(mode="json") if hasattr(initial.result, "model_dump") else initial.result
            await stream_hub.send_to(client, "status", snapshot)

        async def generate() -> AsyncIterator[bytes]:
            yield SSE_RETRY_HINT
            try:
                while not client.disconnected:
                    try:
                        item = await asyncio.wait_for(client.queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield b"event: heartbeat\ndata: {}\n\n"
                        if await request.is_disconnected():
                            break
                        continue
                    stream_hub.record_flush(item)
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
        slotd = await service.call(actor, GetPerf(kind="get_perf"))
        if isinstance(slotd, RpcSuccess) and hasattr(slotd.result, "model_dump"):
            result["slotd"] = slotd.result.model_dump(mode="json")
        return result

    @app.get("/", response_class=FileResponse)
    async def index():
        return assets / "index.html"

    @app.get("/app.js", response_class=FileResponse)
    async def app_script():
        return assets / "app.js"

    @app.get("/styles.css", response_class=FileResponse)
    async def stylesheet():
        return assets / "styles.css"

    app.state.sessions = sessions
    app.state.rpc = rpc_impl
    app.state.event_hub = stream_hub
    app.state.proxy_credential_path = PROXY_CREDENTIAL_PATH
    return app


app = None

build_app = create_app


__all__ = [
    "CONTROL_SOCKET",
    "EventHub",
    "StreamClient",
    "UnixRpcClient",
    "create_app",
    "build_app",
]
