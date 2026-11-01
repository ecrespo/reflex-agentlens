"""Event guard and auth-hook resolution, without Reflex (plan tasks P6-01.2, P6-03.2).

Tech Design §5.5: every page event goes through :func:`guarded`, which maps the API §6.6
errors to UI states. ``StorageUnavailable`` → banner, ``Forbidden`` → "not authorized",
``NotFound``/``ValidationError`` → their own states, anything else → a generic error and one
log line that names the exception class only (REQ-323).

PRD v1.2 REQ-191.a, API v1.4.4 §7.2 (Analyze A-12): a page load always calls the auth hook;
other events may reuse its result for at most :data:`VIEWER_TTL_S` seconds, and only for the
same client token (:class:`ViewerCache`).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from reflex_agentlens.core.errors import Forbidden, NotFound, StorageUnavailable, ValidationError
from reflex_agentlens.query.dto import Viewer

__all__ = [
    "VIEWER_TTL_S",
    "UiError",
    "ViewerCache",
    "classify",
    "guarded",
    "resolve_viewer",
]

log = logging.getLogger("agentlens.ui")

VIEWER_TTL_S = 30.0
T = TypeVar("T")
UiErrorKind = Literal["storage", "forbidden", "not_found", "invalid", "error"]

_MESSAGES: dict[UiErrorKind, str] = {
    "storage": "Storage is unavailable. Retrying…",
    "forbidden": "You are not authorized to see this.",
    "not_found": "Not found.",
    "invalid": "Invalid request.",
    "error": "Something went wrong. Check the server log.",
}


@dataclass(frozen=True, slots=True)
class UiError:
    kind: UiErrorKind
    message: str


def classify(exc: BaseException) -> UiError:
    """The UI state of a failed event (Tech Design §5.5)."""
    if isinstance(exc, StorageUnavailable):
        return UiError("storage", _MESSAGES["storage"])
    if isinstance(exc, Forbidden):
        return UiError("forbidden", _MESSAGES["forbidden"])
    if isinstance(exc, NotFound):
        return UiError("not_found", _MESSAGES["not_found"])
    if isinstance(exc, ValidationError):
        # our own message (field + reason), shown to the viewer who sent the value
        return UiError("invalid", f"{_MESSAGES['invalid']} {exc}")
    log.error("ui event failed (%s)", type(exc).__name__)
    return UiError("error", _MESSAGES["error"])


async def guarded(call: Callable[[], Awaitable[T]]) -> tuple[T | None, UiError | None]:
    """Run one service call; ``(result, None)`` or ``(None, error state)``. Never raises
    (``CancelledError`` and other ``BaseException``s pass through)."""
    try:
        return await call(), None
    except Exception as exc:
        return None, classify(exc)


class _Miss:
    pass


MISS = _Miss()


class ViewerCache:
    """Hook results per client token, reused by events for at most ``ttl_s`` (bounded)."""

    def __init__(
        self,
        *,
        ttl_s: float = VIEWER_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 10_000,
    ) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self.max_clients = max_clients
        self._entries: OrderedDict[str, tuple[float, Viewer | None]] = OrderedDict()

    def get(self, token: str) -> Viewer | _Miss | None:
        entry = self._entries.get(token)
        if entry is None:
            return MISS
        stored_at, viewer = entry
        if self.clock() - stored_at > self.ttl_s:
            del self._entries[token]
            return MISS
        return viewer

    def put(self, token: str, viewer: Viewer | None) -> None:
        self._entries[token] = (self.clock(), viewer)
        self._entries.move_to_end(token)
        while len(self._entries) > self.max_clients:
            self._entries.popitem(last=False)

    def forget(self, token: str) -> None:
        self._entries.pop(token, None)


async def resolve_viewer(
    hook: Callable[[Any], Awaitable[Viewer | None]],
    state: Any,
    token: str,
    *,
    page_load: bool,
    cache: ViewerCache,
) -> Viewer | None:
    """The viewer of one event. Page loads always call the hook; other events reuse a result
    younger than the TTL for the same token. An empty token is never cached."""
    if not page_load and token:
        cached = cache.get(token)
        if not isinstance(cached, _Miss):
            return cached
    viewer = await hook(state)
    if viewer is not None and not isinstance(viewer, Viewer):
        raise TypeError("the auth hook must return reflex_agentlens.Viewer or None")
    if token:
        cache.put(token, viewer)
    return viewer
