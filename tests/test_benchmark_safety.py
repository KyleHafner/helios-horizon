from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from game_control.benchmark_safety import BenchmarkPreflight
from game_control.capability_evidence import WakeSafetyEvidence
from game_control.protocol import ProfileStatus, StatusSnapshot
from game_control.models import HealthState, ObservedState, ProfileId


def _snapshot(now: datetime) -> StatusSnapshot:
    return StatusSnapshot(
        generation=1,
        observed_at=now,
        profiles=(ProfileStatus(
            profile_id=ProfileId.MINECRAFT,
            state=ObservedState.STOPPED,
            health=HealthState.UNKNOWN,
            slot_owner=None,
            active_job_id=None,
            pid=None,
            started_at=None,
            uptime_seconds=None,
            cpu_percent=None,
            rss_bytes=None,
            players_online=0,
            installed_version=None,
            restart_required=False,
            required_ports_ready=False,
        ),),
    )


@pytest.mark.asyncio
async def test_benchmark_preflight_maps_typed_evidence_to_legacy_contract():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE player_sessions (ended_at TEXT)")
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    preflight = BenchmarkPreflight(
        storage_paths=("root",),
        storage_usage=lambda _path: SimpleNamespace(free=10 * 1024**3),
        ups_health=lambda: True,
        session_store=SimpleNamespace(connection=connection),
        wake_evidence=lambda: WakeSafetyEvidence(True, True),
        clock=lambda: now,
    )
    snapshot = _snapshot(now)

    evidence = await preflight.evaluate(
        snapshot=snapshot,
        maintenance_window=True,
        rollback_safe=True,
        public_wake_policy="safe",
    )

    assert set(evidence.legacy_mapping()) == {
        "maintenance_window", "storage_acceptable", "ups_acceptable",
        "quiet_period", "no_wake_session", "no_conflicting_jobs",
        "rollback_safe_public_wake",
    }
    assert all(evidence.legacy_mapping().values())
    assert all(item.state == "available" for item in evidence.items)


@pytest.mark.asyncio
async def test_benchmark_preflight_fails_closed_for_unavailable_safety_inputs():
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    preflight = BenchmarkPreflight(clock=lambda: now, wake_evidence=lambda: True)
    snapshot = _snapshot(now)

    evidence = await preflight.evaluate(
        snapshot=snapshot,
        maintenance_window=True,
        rollback_safe=True,
        public_wake_policy="safe",
    )
    values = evidence.legacy_mapping()
    assert values["storage_acceptable"] is False
    assert values["ups_acceptable"] is False
    assert values["quiet_period"] is False
    assert values["no_wake_session"] is False
