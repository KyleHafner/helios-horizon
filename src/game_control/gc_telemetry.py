"""Bounded parser for Java 17 unified GC pause observability."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

MAX_LINE_BYTES = 16 * 1024
MAX_PAUSE_MS = 10 * 60 * 1000
MAX_COUNTER = 10_000_000
MAX_AGE = 1000
_GC_RE = re.compile(r"GC\((\d+)\)")
_PAUSE_RE = re.compile(r"Pause\s+([A-Za-z]+).*?\s([0-9]+(?:\.[0-9]+)?)ms(?:\s*$|\s+)")
_AGE_RE = re.compile(r"(?:age|Age)[= ](\d+)")
GcKind = Literal["pause", "reset"]
GcType = Literal["young", "full", "mixed", "remark", "cleanup", "unknown"]


@dataclass(frozen=True)
class GcEvent:
    kind: GcKind
    offset: int
    duration_ms: float | None = None
    counter: int | None = None
    age: int | None = None
    gc_type: GcType | None = None


def _gc_type(label: str) -> GcType:
    value = label.lower()
    return value if value in {"young", "full", "mixed", "remark", "cleanup"} else "unknown"  # type: ignore[return-value]


class GcTelemetryParser:
    """Consume bounded incremental lines without retaining raw log content."""

    def __init__(self, *, max_line_bytes: int = MAX_LINE_BYTES):
        if not 1 <= max_line_bytes <= MAX_LINE_BYTES:
            raise ValueError("max_line_bytes out of bounds")
        self.max_line_bytes = max_line_bytes
        self._partial = b""
        self._offset = 0

    def reset(self) -> GcEvent:
        self._partial = b""
        self._offset = 0
        return GcEvent("reset", 0)

    def feed(self, data: bytes, *, rotated: bool = False) -> tuple[GcEvent, ...]:
        if not isinstance(data, bytes):
            raise TypeError("GC feed requires bytes")
        events: list[GcEvent] = []
        if rotated:
            events.append(self.reset())
        buffer = self._partial + data
        cursor = 0
        while True:
            newline = buffer.find(b"\n", cursor)
            if newline < 0:
                self._partial = buffer[cursor:]
                if len(self._partial) > self.max_line_bytes:
                    self._partial = b""
                    raise ValueError("GC line exceeds configured limit")
                break
            raw = buffer[cursor:newline].rstrip(b"\r")
            if len(raw) > self.max_line_bytes:
                raise ValueError("GC line exceeds configured limit")
            event = self._parse(raw.decode("utf-8", "replace"), self._offset + newline + 1)
            self._offset += newline + 1
            if event is not None:
                events.append(event)
            cursor = newline + 1
        return tuple(events)

    def _parse(self, line: str, offset: int) -> GcEvent | None:
        match = _PAUSE_RE.search(line)
        counter_match = _GC_RE.search(line)
        if match is None or counter_match is None:
            return None
        try:
            duration = float(match.group(2))
            counter = int(counter_match.group(1))
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(duration) or not 0 < duration <= MAX_PAUSE_MS or not 0 <= counter <= MAX_COUNTER:
            return None
        age_match = _AGE_RE.search(line)
        age = int(age_match.group(1)) if age_match else None
        if age is not None and age > MAX_AGE:
            return None
        return GcEvent("pause", offset, duration, counter, age, _gc_type(match.group(1)))


__all__ = ["GcEvent", "GcTelemetryParser"]
