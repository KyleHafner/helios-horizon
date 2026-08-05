from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..models import Profile
from ..protocol import LogLine


@dataclass(frozen=True)
class AdapterObservation:
    """Safe adapter state; upstream payloads never cross this boundary."""

    running: bool
    healthy: bool | None = None
    pid: int | None = None
    started_at: datetime | None = None
    players_online: int | None = None
    player_names: tuple[str, ...] | None = None
    installed_version: str | None = None
    required_ports_ready: bool | None = None


class AdapterError(RuntimeError):
    """Redacted, stable adapter failure."""

    def __init__(self, message: str, *, retryable: bool = True, returncode: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.returncode = returncode


class Adapter(Protocol):
    async def observe(self, profile: Profile) -> AdapterObservation: ...

    async def start(self, profile: Profile) -> None: ...

    async def graceful_stop(self, profile: Profile) -> None: ...

    async def stop(self, profile: Profile) -> None: ...

    async def force_stop(self, profile: Profile) -> None: ...

    async def send_command(self, profile: Profile, command: str) -> None: ...

    async def recent_logs(
        self,
        profile: Profile,
        limit: int,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[LogLine]: ...
