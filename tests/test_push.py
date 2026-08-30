import asyncio

import pytest

from game_control.push import ReadWriteLock, WatchCursor, WatchHub, WatchProtocolError


@pytest.mark.asyncio
async def test_watch_replay_cursor_and_full_snapshot():
    hub = WatchHub(history_size=2)
    await hub.publish("status_delta", {"state": "starting"}, generation=1)
    snapshot = await hub.publish("status", {"state": "ready"}, generation=2, full=True)
    client = await hub.subscribe(WatchCursor(sequence=0, generation=0))
    assert (await client.queue.get()).sequence == snapshot.sequence


@pytest.mark.asyncio
async def test_watch_rejects_stale_generation_and_duplicate_order():
    hub = WatchHub()
    await hub.publish("status", {}, generation=4, full=True)
    with pytest.raises(WatchProtocolError):
        await hub.publish("status_delta", {}, generation=3)
    await hub.publish("status_delta", {}, generation=4)
    assert hub.generation == 4


@pytest.mark.asyncio
async def test_watch_overflow_resyncs_then_evicts_slow_client():
    hub = WatchHub(queue_size=1, max_overflows=2)
    client = await hub.subscribe()
    await hub.publish("status", {}, generation=1, full=True)
    await hub.publish("status_delta", {"n": 2}, generation=2)
    assert (await client.queue.get()).kind == "full_resync"
    await hub.publish("status_delta", {"n": 3}, generation=3)
    await hub.publish("status_delta", {"n": 4}, generation=4)
    assert client.disconnected


@pytest.mark.asyncio
async def test_full_resync_reserves_unique_cursor_before_next_real_event():
    hub = WatchHub(queue_size=1)
    client = await hub.subscribe()
    first = await hub.publish("status", {}, generation=1, full=True)
    await hub.publish("status_delta", {"n": 2}, generation=2)
    marker = await client.queue.get()
    next_event = await hub.publish("status_delta", {"n": 3}, generation=3)
    following = await client.queue.get()
    assert marker.kind == "full_resync"
    assert marker.sequence > first.sequence
    assert next_event.sequence > marker.sequence
    assert following.sequence == next_event.sequence
    assert len({first.sequence, marker.sequence, next_event.sequence}) == 3


@pytest.mark.asyncio
async def test_empty_history_resync_cursor_is_not_reused():
    hub = WatchHub(queue_size=1)
    client = await hub.subscribe(WatchCursor(sequence=99, generation=99))
    marker = await client.queue.get()
    event = await hub.publish("status", {}, generation=100, full=True)
    assert marker.kind == "full_resync"
    assert event.sequence > marker.sequence


@pytest.mark.asyncio
async def test_watch_heartbeat_is_bounded_and_does_not_mutate_sequence():
    hub = WatchHub()
    client = await hub.subscribe()
    before = hub._sequence
    event = await hub.heartbeat(client, timeout=0.001)
    assert event.kind == "heartbeat"
    assert event.sequence == before


@pytest.mark.asyncio
async def test_stalled_client_does_not_block_publisher():
    hub = WatchHub(queue_size=1)
    slow = await hub.subscribe()
    fast = await hub.subscribe()
    await hub.publish("status", {}, generation=1, full=True)
    await hub.publish("status_delta", {}, generation=2)
    assert (await fast.queue.get()).kind == "full_resync"
    assert slow.overflow_count == 1


@pytest.mark.asyncio
async def test_read_write_lock_excludes_writer_and_records_timing():
    lock = ReadWriteLock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def reader():
        async with lock.read():
            entered.set()
            await release.wait()

    reader_task = asyncio.create_task(reader())
    await entered.wait()
    writer_task = asyncio.create_task(_writer(lock))
    await asyncio.sleep(0)
    assert not writer_task.done()
    release.set()
    await asyncio.gather(reader_task, writer_task)
    timing = lock.timing()
    assert timing.read_count == timing.write_count == 1
    assert timing.write_wait_ms >= 0


async def _writer(lock):
    async with lock.write():
        return None
