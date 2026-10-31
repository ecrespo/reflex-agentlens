"""In-process change feed and NOTIFY payload (plan tasks P2-10.1, P2-10.2; API §5.2, DD-007)."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from reflex_agentlens.store.changefeed import (
    NOTIFY_MAX_BYTES,
    ChangeFeed,
    decode_notify,
    encode_notify,
    events_for,
)
from reflex_agentlens.store.records import ChangeEvent, RunKey


def key(project: str, n: int) -> RunKey:
    return RunKey(project, n.to_bytes(16, "big"))


def event(project: str = "A", *keys: int) -> ChangeEvent:
    return ChangeEvent.build("spans_committed", project, [key(project, k) for k in keys], 7)


async def next_event(sub: object, within: float = 1.0) -> ChangeEvent:
    return await asyncio.wait_for(anext(sub), within)  # type: ignore[call-overload]


def test_events_for_groups_by_project_and_caps_keys() -> None:
    keys = [key("B", 1), key("A", 2), key("A", 1), key("A", 1)]
    evs = events_for("purged", keys, 9)
    assert [(e.project_id, e.run_keys, e.truncated) for e in evs] == [
        ("A", (key("A", 1), key("A", 2)), False),
        ("B", (key("B", 1),), False),
    ]
    big = events_for("spans_committed", [key("A", i) for i in range(250)], 9)[0]
    assert (len(big.run_keys), big.truncated) == (200, True)  # API §5.2 cap
    assert events_for("purged", [], 9) == []


async def test_subscribers_get_matching_events_only() -> None:
    feed = ChangeFeed()
    all_sub, only_b = feed.subscribe(), feed.subscribe(frozenset({"B"}))
    await feed.publish(event("A", 1))
    feed.publish_nowait(event("B", 2))
    assert (await next_event(all_sub)).project_id == "A"
    assert (await next_event(all_sub)).project_id == "B"
    assert (await next_event(only_b)).project_id == "B"
    assert feed.subscriber_count == 2
    all_sub.close()
    async with only_b:
        pass
    assert feed.subscriber_count == 0
    with pytest.raises(StopAsyncIteration):
        await next_event(all_sub)
    await feed.publish(event("A"))  # no subscribers: nothing happens


async def test_publish_from_another_thread() -> None:
    """The ingest thread runs its own loop; delivery hops onto the subscriber's loop."""
    feed = ChangeFeed()
    sub = feed.subscribe()

    def ingest_thread() -> None:
        asyncio.run(feed.publish(event("A", 1)))

    worker = threading.Thread(target=ingest_thread)
    worker.start()
    worker.join()
    assert (await next_event(sub)).run_keys == (key("A", 1),)


async def test_full_queue_drops_and_close_still_ends_iteration() -> None:
    feed = ChangeFeed(queue_size=2)
    sub = feed.subscribe()
    for i in range(5):
        feed.publish_nowait(event("A", i))
    await asyncio.sleep(0)
    assert sub.dropped == 3  # lossy by design
    sub.close()
    await asyncio.sleep(0)
    got = [e async for e in sub]
    assert len(got) == 1  # one event made way for the end marker
    assert sub.dropped == 4


def test_subscriber_on_a_closed_loop_is_removed() -> None:
    feed = ChangeFeed()

    async def subscribe() -> None:
        feed.subscribe()

    asyncio.run(subscribe())
    assert feed.subscriber_count == 1
    feed.publish_nowait(event("A"))
    assert feed.subscriber_count == 0


def test_notify_payload_round_trip() -> None:
    ev = event("01PROJECT", 1, 2)
    notice = decode_notify(encode_notify(ev, "origin", "schema_a"))
    assert notice is not None
    assert (notice.origin_id, notice.namespace, notice.event) == ("origin", "schema_a", ev)


def test_notify_payload_is_bounded() -> None:
    """DD-007: above 7,900 bytes the run keys are dropped and the event marked truncated."""
    ev = event("01HZZZZZZZZZZZZZZZZZZZZZZZ", *range(200))
    full = decode_notify(encode_notify(ev, "01HZZZZZZZZZZZZZZZZZZZZZZZ", "agentlens"))
    assert full is not None
    assert full.event == ev  # 200 capped keys fit in a normal payload
    payload = encode_notify(ev, "o", "x" * 1_000)
    assert len(payload.encode()) <= NOTIFY_MAX_BYTES
    notice = decode_notify(payload)
    assert notice is not None
    assert (notice.event.run_keys, notice.event.truncated) == ((), True)


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        json.dumps({"o": "x"}),
        json.dumps({"o": "x", "n": "", "k": "bogus", "p": "A", "r": [], "t": False, "c": 1}),
        json.dumps({"o": "x", "n": "", "k": "purged", "p": "A", "r": ["zz"], "t": False, "c": 1}),
    ],
)
def test_malformed_payloads_are_ignored(payload: str) -> None:
    assert decode_notify(payload) is None
