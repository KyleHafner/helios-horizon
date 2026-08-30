import asyncio

import pytest

from game_control.web_main import AdaptiveStatusCadence, EventHub, StreamClient, _last_event_id


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
async def test_new_subscriber_receives_last_authoritative_cached_full_snapshot():
    hub = EventHub()
    await hub.publish("status", {"generation": 1, "profiles": [{"profile_id": "minecraft"}]})
    await hub.publish("status", {"generation": 2, "profiles": [{"profile_id": "minecraft", "state": "running"}]})
    await hub.cache_status({"generation": 2, "profiles": [{"profile_id": "minecraft", "state": "running"}]})

    client = await hub.subscribe(last_event_id=1)
    item = await asyncio.wait_for(client.queue.get(), timeout=1)

    assert item["id"] == 2
    assert item["data"]["generation"] == 2
    assert item["data"]["profiles"][0]["state"] == "running"


@pytest.mark.asyncio
async def test_empty_cached_snapshot_fails_closed_without_synthetic_status():
    hub = EventHub()

    client = await hub.subscribe()

    assert client.queue.empty()


@pytest.mark.asyncio
async def test_cached_snapshot_reconnect_replay_and_subscriber_isolation():
    hub = EventHub()
    published = await hub.publish("status", {"generation": 3})
    first = await hub.subscribe(last_event_id=published["id"] - 1)
    second = await hub.subscribe(last_event_id=published["id"])

    assert (await first.queue.get())["id"] == published["id"]
    assert second.queue.empty()
    await hub.cache_status({"generation": 4, "profiles": []})
    third = await hub.subscribe(last_event_id=published["id"] - 1)
    assert (await third.queue.get())["data"]["generation"] == 4
    assert first.queue.empty() and second.queue.empty()


@pytest.mark.asyncio
async def test_publisher_failure_clears_cached_snapshot():
    hub = EventHub()
    await hub.publish("status", {"generation": 7})
    await hub.clear_cached_status()

    client = await hub.subscribe()

    assert client.queue.empty()


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


@pytest.mark.asyncio
async def test_send_to_replaces_oldest_frame_when_client_queue_overflows():
    hub = EventHub(max_queue=1)
    client = await hub.subscribe()

    await hub.send_to(client, "status", {"generation": 1})
    await hub.send_to(client, "status", {"generation": 2})

    assert (await client.queue.get())["data"]["generation"] == 2
    assert len(hub._history) == 0


@pytest.mark.asyncio
async def test_unsubscribe_removes_client_and_disconnects_future_delivery():
    hub = EventHub()
    client = await hub.subscribe()

    await hub.unsubscribe(client)
    await hub.publish("status", {"generation": 1})
    await hub.send_to(client, "status", {"generation": 2})

    assert client.disconnected
    assert not hub._clients
    assert client.queue.empty()


@pytest.mark.asyncio
async def test_publish_tolerates_queue_draining_between_full_and_drop():
    class DrainedQueue(asyncio.Queue):
        def __init__(self):
            super().__init__(maxsize=1)
            self._first_put = True

        def put_nowait(self, item):
            if self._first_put:
                self._first_put = False
                raise asyncio.QueueFull
            return super().put_nowait(item)

        def get_nowait(self):
            raise asyncio.QueueEmpty

    hub = EventHub()
    queue = DrainedQueue()
    client = StreamClient(queue)
    hub._clients.add(client)

    item = await hub.publish("status", {"generation": 1})

    assert not client.disconnected
    assert queue.get_nowait.__self__ is queue
    assert queue.qsize() == 1
    assert queue._queue[0] == item


def test_last_event_id_is_bounded_and_invalid_values_restart_from_zero():
    from starlette.requests import Request

    def request_with(value, *, after=None):
        query = b"?after=" + after.encode() if after is not None else b""
        return Request({"type": "http", "path": "/api/v1/stream", "query_string": query,
                        "headers": [(b"last-event-id", value.encode())]})

    assert _last_event_id(request_with("not-an-id")) == 0
    assert _last_event_id(request_with("-5")) == 0
    assert _last_event_id(request_with(str(2**63 + 1))) == 0


def test_query_cursor_is_bounded_and_valid_native_header_wins():
    from starlette.requests import Request

    def query(value, headers=()):
        return Request({"type": "http", "path": "/api/v1/stream",
                        "query_string": f"after={value}".encode(),
                        "headers": list(headers)})

    assert _last_event_id(query("41")) == 41
    assert _last_event_id(query("-1")) == 0
    assert _last_event_id(query("01")) == 0
    assert _last_event_id(query("x")) == 0
    assert _last_event_id(query(str(2**63 + 1))) == 0
    assert _last_event_id(query("41", [(b"last-event-id", b"42")])) == 42
    assert _last_event_id(query("42", [(b"last-event-id", b"bad")])) == 0


def test_absent_native_header_allows_valid_query_cursor():
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/v1/stream",
                       "query_string": b"after=42", "headers": []})
    assert _last_event_id(request) == 42


@pytest.mark.parametrize("header", [b"", b"bad", b"01", b"-1", str(2**63 + 1).encode()])
def test_present_invalid_native_header_fails_closed_even_with_valid_query(header):
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/v1/stream",
                       "query_string": b"after=42",
                       "headers": [(b"last-event-id", header)]})
    assert _last_event_id(request) == 0


@pytest.mark.parametrize("value", ["", "00", "0009", " ", " 1", "1 ", "\t1", "+1", "-1", "1.0", "1e3", "0x10"])
def test_sse_cursor_rejects_noncanonical_decimal_query_values(value):
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/v1/stream",
                       "query_string": f"after={value}".encode(), "headers": []})
    assert _last_event_id(request) == 0


@pytest.mark.parametrize("value,expected", [("0", 0), ("1", 1), ("9", 9),
                                               ("10", 10), ("9007199254740991", 9007199254740991),
                                               ("9223372036854775807", 2**63 - 1),
                                               ("9223372036854775808", 0)])
def test_sse_cursor_accepts_only_bounded_ascii_decimal_values(value, expected):
    from starlette.requests import Request

    request = Request({"type": "http", "path": "/api/v1/stream",
                       "query_string": f"after={value}".encode(), "headers": []})
    assert _last_event_id(request) == expected


@pytest.mark.asyncio
async def test_eventhub_cursor_does_not_replay_received_id_but_newer_event_arrives():
    hub = EventHub()
    published = await hub.publish("status", {"generation": 1})
    resumed = await hub.subscribe(last_event_id=published["id"])
    assert resumed.queue.empty()
    newer = await hub.publish("status", {"generation": 2})
    assert (await resumed.queue.get())["id"] == newer["id"]
