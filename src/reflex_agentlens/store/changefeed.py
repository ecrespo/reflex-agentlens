"""Change feed: in-process broadcaster and the PostgreSQL wire format (plan tasks P2-10.1, P2-10.2).

REQ-140.a (storage side), DD-007, API §5.2.

- :class:`ChangeFeed` fans events out to subscribers. ``publish_nowait`` is **thread-safe**: the
  ingest thread runs its own event loop, so each subscriber's queue is fed on the subscriber's
  loop through ``call_soon_threadsafe``.
- Delivery is at-most-once and lossy by design: a subscriber whose queue is full loses the
  event. Subscribers treat an event as "something changed, re-query", never as data.
- Backends publish **after** the commit (a rolled-back transaction publishes nothing). On
  PostgreSQL the same events also travel through ``pg_notify`` inside the commit transaction;
  :func:`encode_notify` / :func:`decode_notify` define that payload (≤ 7,900 bytes, otherwise
  ``truncated`` with no run keys).
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, get_args

from reflex_agentlens.store.records import ChangeEvent, ChangeKind, ProjectScope, RunKey

__all__ = [
    "NOTIFY_CHANNEL",
    "NOTIFY_MAX_BYTES",
    "ChangeFeed",
    "Notice",
    "Subscription",
    "decode_notify",
    "encode_notify",
    "events_for",
]

NOTIFY_CHANNEL = "agentlens_changes"
NOTIFY_MAX_BYTES = 7_900
DEFAULT_QUEUE_SIZE = 1_000
_KINDS = frozenset(get_args(ChangeKind))


def events_for(kind: ChangeKind, keys: Iterable[RunKey], committed_at_ns: int) -> list[ChangeEvent]:
    """One event per Project (API §5.1 step 6), Run keys sorted and capped at 200."""
    by_project: dict[str, list[RunKey]] = {}
    for key in keys:
        by_project.setdefault(key.project_id, []).append(key)
    return [
        ChangeEvent.build(kind, project, sorted(set(project_keys)), committed_at_ns)
        for project, project_keys in sorted(by_project.items())
    ]


class Subscription:
    """An async iterator of events for a set of Projects (``None`` = every Project).

    Registered when created, so nothing published after :meth:`ChangeFeed.subscribe` returns is
    missed. Use it as an async context manager or call :meth:`close`.
    """

    def __init__(self, feed: ChangeFeed, project_ids: ProjectScope | None, maxsize: int) -> None:
        self._feed = feed
        self.project_ids = project_ids
        self.loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[ChangeEvent | None] = asyncio.Queue(maxsize)
        self.dropped = 0  # events lost to a full queue (lossy by design)

    def wants(self, event: ChangeEvent) -> bool:
        return self.project_ids is None or event.project_id in self.project_ids

    def _put(self, event: ChangeEvent | None) -> None:
        if event is None and self._queue.full():  # the end marker must always get in
            self._queue.get_nowait()
            self.dropped += 1
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> ChangeEvent:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        self._feed._remove(self)
        self.loop.call_soon_threadsafe(self._put, None)

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()


class ChangeFeed:
    """In-process fan-out (API §5.2 ``ChangeFeed``)."""

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self.queue_size = queue_size

    def subscribe(self, project_ids: ProjectScope | None = None) -> Subscription:
        sub = Subscription(self, project_ids, self.queue_size)
        with self._lock:
            self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish_nowait(self, event: ChangeEvent) -> None:
        """Deliver to every matching subscriber; safe from any thread, never blocks."""
        with self._lock:
            targets = [sub for sub in self._subs if sub.wants(event)]
        for sub in targets:
            try:
                sub.loop.call_soon_threadsafe(sub._put, event)
            except RuntimeError:  # the subscriber's loop is closed
                self._remove(sub)

    async def publish(self, event: ChangeEvent) -> None:
        self.publish_nowait(event)


# ── PostgreSQL NOTIFY payload (P2-10.2) ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Notice:
    """A decoded ``pg_notify`` payload: the event plus where it came from."""

    origin_id: str
    namespace: str
    event: ChangeEvent


def encode_notify(event: ChangeEvent, origin_id: str, namespace: str) -> str:
    """JSON payload; above ``NOTIFY_MAX_BYTES`` the run keys are dropped and ``truncated`` set.

    ``namespace`` is the backend's ``search_path`` (empty by default): several deployments in
    different schemas of one database share the channel but ignore each other's events.
    """
    body: dict[str, Any] = {
        "o": origin_id,
        "n": namespace,
        "k": event.kind,
        "p": event.project_id,
        "r": [key.trace_id.hex() for key in event.run_keys],
        "t": event.truncated,
        "c": event.committed_at_ns,
    }
    text = json.dumps(body, separators=(",", ":"))
    if len(text.encode()) > NOTIFY_MAX_BYTES:
        body["r"], body["t"] = [], True
        text = json.dumps(body, separators=(",", ":"))
    return text


def decode_notify(payload: str) -> Notice | None:
    """Parse a payload; anything malformed is ignored (``None``), never raised."""
    try:
        body = json.loads(payload)
        if body["k"] not in _KINDS:
            return None
        project = str(body["p"])
        event = ChangeEvent(
            kind=body["k"],
            project_id=project,
            run_keys=tuple(RunKey(project, bytes.fromhex(t)) for t in body["r"]),
            truncated=bool(body["t"]),
            committed_at_ns=int(body["c"]),
        )
        return Notice(origin_id=str(body["o"]), namespace=str(body["n"]), event=event)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
