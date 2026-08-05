import asyncio

import pytest

from game_control.web_main import AdaptiveStatusCadence, EventHub


def test_adaptive_status_cadence_backs_off_idle_and_resets_on_transition():
    cadence = AdaptiveStatusCadence(fast_interval=3.0, idle_interval=15.0)
    idle = {"profiles": [{"profile_id": "minecraft", "slot_owner": "minecraft", "players_online": 0}]}
    active = {"profiles": [{"profile_id": "minecraft", "slot_owner": "minecraft", "players_online": 2}]}

    assert cadence.interval_for(idle) == 15.0
    assert cadence.interval_for(idle) == 15.0
    assert cadence.interval_for(active) == 3.0
    assert cadence.interval_for(active) == 3.0
    assert cadence.interval_for(idle) == 3.0
    assert cadence.interval_for(idle) == 15.0


@pytest.mark.asyncio
async def test_sse_event_ids_resume_and_bounded_queue():
    hub = EventHub(max_queue=1)
    client = await hub.subscribe(last_event_id=0)
    await hub.publish("status", {"message": "safe"})
    event = await asyncio.wait_for(client.queue.get(), timeout=1)
    assert event["id"] == 1
    await hub.publish("status", {"message": "second"})
    await hub.publish("status", {"message": "third"})
    assert not client.disconnected
    assert (await asyncio.wait_for(client.queue.get(), timeout=1))["data"]["message"] == "third"


@pytest.mark.asyncio
async def test_sse_redacts_credentials_and_dom_instructions():
    hub = EventHub()
    client = await hub.subscribe()
    await hub.publish("log", {"message": "Bearer abcdefghijklmnop", "html": "<script>", "view_reset": True})
    item = await client.queue.get()
    assert "Bearer" not in item["data"]["message"]
    assert "html" not in item["data"] and "view_reset" not in item["data"]


@pytest.mark.asyncio
async def test_sse_replay_is_trimmed_to_client_queue_bound():
    hub = EventHub(max_queue=2, max_history=64)
    for index in range(5):
        await hub.publish("status", {"generation": index})
    client = await hub.subscribe()
    assert not client.disconnected
    replay = await asyncio.wait_for(client.queue.get(), timeout=1)
    assert replay["data"]["generation"] == 4


@pytest.mark.asyncio
async def test_sse_fresh_client_survives_history_overflow_and_receives_new_frame():
    hub = EventHub(max_queue=4, max_history=64)
    for index in range(40):
        await hub.publish("status", {"generation": index})
    client = await hub.subscribe()
    assert not client.disconnected
    await hub.publish("status", {"generation": 40})
    events = [await asyncio.wait_for(client.queue.get(), timeout=1) for _ in range(2)]
    assert events[-1]["data"]["generation"] == 40
    assert not client.disconnected


@pytest.mark.asyncio
async def test_one_client_snapshot_does_not_broadcast():
    hub = EventHub()
    first = await hub.subscribe()
    await hub.send_to(first, "status", {"generation": 1})
    assert (await first.queue.get())["data"]["generation"] == 1
    second = await hub.subscribe()
    await hub.send_to(second, "status", {"generation": 2})
    assert (await second.queue.get())["data"]["generation"] == 2
    assert first.queue.empty()
    assert len(hub._history) == 0
