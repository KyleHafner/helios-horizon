"""Bounded, redacted log access and safe diagnostics."""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping

from .adapters.base import AdapterError
from .protocol import LogLine
from .redaction import Redactor

_MAX_BYTES = 256 * 1024
_MAX_LINES = 5000
_MAX_QUERY = 256
_MAX_AGE = timedelta(hours=24)
_BROWSER_MARKERS = ("/.mozilla/", "/.config/google-chrome/", "/.config/chromium/", "/Cookies", "/Local State")


def clamp_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), _MAX_LINES))
    except (TypeError, ValueError, OverflowError):
        return 1


def clamp_since(value: datetime | None, *, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(value, current - _MAX_AGE)


class LogService:
    def __init__(self, adapter: Any | None = None, *, redactor: Redactor | None = None):
        self.adapter = adapter
        self.redactor = redactor or Redactor()

    def tail_from_paths(
        self,
        paths: Iterable[str | Path],
        *,
        limit: int = 500,
        since: datetime | None = None,
    ) -> tuple[LogLine, ...]:
        bounded = clamp_limit(limit)
        cutoff = clamp_since(since)
        lines: list[LogLine] = []
        for raw in islice(paths, 32):
            path = Path(raw)
            if any(marker in str(path) for marker in _BROWSER_MARKERS):
                continue
            try:
                data = _read_tail(path, _MAX_BYTES)
            except (OSError, ValueError):
                continue
            now = datetime.now(timezone.utc)
            redacted_buffer = self.redactor.redact(data.decode("utf-8", "replace"))
            for text in redacted_buffer.splitlines()[-bounded:]:
                timestamp = _parse_timestamp(text)
                if cutoff is not None and (timestamp is None or timestamp < cutoff):
                    continue
                lines.append(LogLine(timestamp=timestamp or now, severity="info", message=text[:8192]))
        return tuple(lines[-bounded:])

    async def tail(
        self,
        profile: Any,
        *,
        limit: int = 500,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[LogLine, ...]:
        bounded = clamp_limit(limit)
        cutoff = clamp_since(since)
        paths = getattr(getattr(profile, "paths", None), "log_files", ())
        output: list[LogLine] = []
        if self.adapter is not None and hasattr(self.adapter, "recent_logs"):
            try:
                if since is None and until is None:
                    value = self.adapter.recent_logs(profile, bounded)
                else:
                    try:
                        value = self.adapter.recent_logs(profile, bounded, since=since, until=until)
                    except TypeError:
                        # Keep older adapter test seams and third-party adapters usable;
                        # the service still applies the range client-side below.
                        value = self.adapter.recent_logs(profile, bounded)
                value = await value if inspect.isawaitable(value) else value
                source = value if isinstance(value, (list, tuple)) else ()
                redacted_lines = self.redactor.redact_records(
                    line.message if isinstance(line, LogLine) else str(line) for line in source
                )
                for line, redacted_message in zip(source, redacted_lines):
                    if not isinstance(line, LogLine):
                        continue
                    if line.timestamp.tzinfo is None:
                        continue
                    if cutoff is not None:
                        timestamp = line.timestamp
                        if timestamp.tzinfo is None or timestamp < cutoff:
                            continue
                    if until is not None and line.timestamp > until:
                        continue
                    output.append(
                        LogLine(
                            timestamp=line.timestamp,
                            severity=line.severity,
                            message=redacted_message,
                        )
                    )
            except (AdapterError, RuntimeError, OSError, ValueError, TypeError):
                pass
        if not output:
            output.extend(self.tail_from_paths(paths, limit=bounded, since=since))
        output.sort(key=lambda line: line.timestamp)
        return tuple(output[-bounded:])

    def search_lines(
        self,
        lines: Iterable[str | LogLine],
        query: str,
        *,
        regex: bool = False,
        limit: int = 100,
    ) -> tuple[LogLine, ...]:
        bounded = clamp_limit(limit)
        if not isinstance(query, str) or not query or len(query) > _MAX_QUERY:
            return ()
        try:
            # Search is deliberately literal-only.  Accepting a regex flag
            # from an RPC caller would turn bounded input into a regex DoS.
            matcher = re.compile(re.escape(query), re.IGNORECASE)
        except re.error:
            return ()
        now = datetime.now(timezone.utc)
        output: list[LogLine] = []
        for raw in islice(lines, _MAX_LINES):
            if isinstance(raw, LogLine):
                timestamp, severity, text = raw.timestamp, raw.severity, raw.message
            else:
                timestamp, severity, text = now, "info", str(raw)
            text = self.redactor.redact(text)[:8192]
            if matcher.search(text):
                output.append(LogLine(timestamp=timestamp, severity=severity, message=text))
                if len(output) >= bounded:
                    break
        return tuple(output)

    async def search(
        self,
        profile: Any,
        query: str,
        *,
        regex: bool = False,
        limit: int = 100,
    ) -> tuple[LogLine, ...]:
        try:
            lines = await self.tail(profile, limit=_MAX_LINES)
        except (AdapterError, RuntimeError):
            paths = getattr(getattr(profile, "paths", None), "log_files", ())
            lines = self.tail_from_paths(paths, limit=_MAX_LINES)
        return self.search_lines(lines, query, regex=regex, limit=limit)

    def diagnostics(self, data: Mapping[str, Any] | Any, **kwargs: Any) -> dict[str, Any]:
        """Return only explicitly allowlisted operational fields."""

        if not isinstance(data, Mapping):
            if hasattr(data, "model_dump"):
                data = data.model_dump()
            elif hasattr(data, "__dict__"):
                data = vars(data)
            else:
                data = {}
        allowed = {
            "profile_id",
            "state",
            "health",
            "slot_owner",
            "active_job_id",
            "pid",
            "started_at",
            "uptime_seconds",
            "cpu_percent",
            "rss_bytes",
            "players_online",
            "installed_version",
            "required_ports_ready",
            "evidence",
            "relay",
        }
        result: dict[str, Any] = {}
        for key in allowed:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                result[key] = self.redactor.redact(str(value)) if isinstance(value, str) else value
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, (list, tuple)):
                result[key] = tuple(self.redactor.redact(str(item)) for item in value[:32])
        return result


__all__ = ["LogService", "clamp_limit", "clamp_since"]


def _read_tail(path: Path, maximum: int) -> bytes:
    """Read at most ``maximum`` bytes from a file's end.

    When the window starts in the middle of a line, discard that partial line
    so callers never expose a misleading truncated record.
    """

    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        offset = max(0, size - maximum)
        stream.seek(offset)
        data = stream.read(maximum)
    if offset:
        _, _, remainder = data.partition(b"\n")
        return remainder
    return data


def _parse_timestamp(line: str) -> datetime | None:
    token = line.strip().split(maxsplit=1)[0] if line.strip() else ""
    if not token:
        return None
    try:
        value = datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
