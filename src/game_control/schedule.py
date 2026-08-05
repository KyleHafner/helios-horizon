"""Small, dependency-free cron matching for startup-loaded slot schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .models import ProfileId


@dataclass(frozen=True)
class _CronField:
    values: frozenset[int]
    minimum: int
    maximum: int

    @classmethod
    def parse(cls, value: str, minimum: int, maximum: int) -> "_CronField":
        if not isinstance(value, str) or not value:
            raise ValueError("invalid cron field")
        result: set[int] = set()
        for part in value.split(","):
            base, _, step_text = part.partition("/")
            step = int(step_text) if step_text else 1
            if step <= 0:
                raise ValueError("invalid cron step")
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                left, right = base.split("-", 1)
                start, end = int(left), int(right)
            else:
                start = end = int(base)
            if start < minimum or end > maximum or start > end:
                raise ValueError("cron field out of range")
            result.update(range(start, end + 1, step))
        return cls(frozenset(result), minimum, maximum)

    def matches(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True)
class ScheduleEntry:
    cron: str
    profile: ProfileId
    minute: _CronField
    hour: _CronField
    day: _CronField
    month: _CronField
    weekday: _CronField

    def matches(self, now: datetime) -> bool:
        return (
            self.minute.matches(now.minute)
            and self.hour.matches(now.hour)
            and self.day.matches(now.day)
            and self.month.matches(now.month)
            and self.weekday.matches((now.weekday() + 1) % 7)
        )


def parse_schedule(raw: Any) -> tuple[ScheduleEntry, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("schedule must be an array")
    entries = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"cron", "profile"}:
            raise ValueError("invalid schedule entry")
        cron = item["cron"]
        fields = cron.split() if isinstance(cron, str) else []
        if len(fields) != 5:
            raise ValueError("cron must have five fields")
        try:
            profile = ProfileId(item["profile"])
            parsed = tuple(
                _CronField.parse(value, low, high)
                for value, low, high in zip(fields, (0, 0, 1, 1, 0), (59, 23, 31, 12, 6))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid schedule entry") from exc
        entries.append(ScheduleEntry(cron, profile, *parsed))
    return tuple(entries)


class ScheduleBook:
    def __init__(self, entries: Iterable[ScheduleEntry] = ()):
        self.entries = tuple(entries)
        self._last_minute: tuple[int, int, int, int, int] | None = None

    def due(self, now: datetime) -> tuple[ScheduleEntry, ...]:
        key = (now.year, now.month, now.day, now.hour, now.minute)
        if key == self._last_minute:
            return ()
        self._last_minute = key
        return tuple(entry for entry in self.entries if entry.matches(now))


__all__ = ["ScheduleBook", "ScheduleEntry", "parse_schedule"]
