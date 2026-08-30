import asyncio

import pytest

from game_control.telemetry_sampler import SamplerInvariantError, TelemetrySampler


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_fixed_cadence_and_shutdown():
    clock = FakeClock()
    calls = []

    async def sample():
        calls.append(clock.now)
        if len(calls) == 3:
            sampler._stop.set()

    sampler = TelemetrySampler(sample, interval_seconds=5, monotonic=lambda: clock.now, sleep=clock.sleep)
    await sampler.run()
    assert calls == [0.0, 5.0, 10.0]
    assert clock.sleeps == [5.0, 5.0]
    assert sampler.health()["closed"] is True


@pytest.mark.asyncio
async def test_overrun_coalesces_missed_deadlines_without_burst():
    clock = FakeClock()
    calls = []

    async def sample():
        calls.append(clock.now)
        if len(calls) == 1:
            clock.now += 13.0
        elif len(calls) == 2:
            sampler._stop.set()

    sampler = TelemetrySampler(sample, interval_seconds=5, monotonic=lambda: clock.now, sleep=clock.sleep)
    await sampler.run()
    assert calls == [0.0, 15.0]
    assert sampler.health()["missed_deadlines"] == 2
    assert clock.sleeps == [2.0]


@pytest.mark.asyncio
async def test_ordinary_failures_are_bounded_and_health_recovers():
    clock = FakeClock()
    outcomes = iter([OSError("one"), OSError("two"), None])

    async def sample():
        outcome = next(outcomes)
        if outcome:
            raise outcome
        sampler._stop.set()

    sampler = TelemetrySampler(sample, monotonic=lambda: clock.now, sleep=clock.sleep)
    await sampler.run()
    health = sampler.health()
    assert (health["failures"], health["successes"], health["consecutive_failures"]) == (2, 1, 0)
    assert health["last_error"] is None


@pytest.mark.asyncio
async def test_invariant_failure_propagates_and_closes():
    async def sample():
        raise SamplerInvariantError("bad contract")

    sampler = TelemetrySampler(sample)
    with pytest.raises(SamplerInvariantError):
        await sampler.run()
    assert sampler.health()["closed"] is True
    assert sampler.health()["cycles"] == 1


@pytest.mark.asyncio
async def test_explicit_shutdown_cancels_supervised_task():
    started = asyncio.Event()

    async def sample():
        started.set()
        await asyncio.sleep(60)

    sampler = TelemetrySampler(sample, interval_seconds=60)
    task = sampler.start()
    await started.wait()
    await sampler.shutdown()
    assert task.done()
    assert sampler.health()["closed"] is True


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_callback_task():
    started = asyncio.Event()
    finished = asyncio.Event()

    async def sample():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            finished.set()

    sampler = TelemetrySampler(sample, interval_seconds=60)
    task = sampler.start()
    await started.wait()
    await sampler.shutdown()
    assert task.done()
    assert finished.is_set()
    assert sampler._callback_task is None


@pytest.mark.asyncio
async def test_duration_ring_and_age_are_bounded():
    clock = FakeClock()
    count = 0

    async def sample():
        nonlocal count
        count += 1
        clock.now += count / 1000
        if count == 4:
            sampler._stop.set()

    sampler = TelemetrySampler(sample, monotonic=lambda: clock.now, sleep=clock.sleep, duration_capacity=2)
    await sampler.run()
    health = sampler.health(now=clock.now + 2)
    assert health["cycle_duration_samples"] == 2
    assert health["last_cycle_age_ms"] == pytest.approx(2000)
    assert health["cycle_duration_p95_ms"] >= health["last_cycle_duration_ms"]
