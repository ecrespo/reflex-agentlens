"""Ingest admission: API keys and the verification cache (plan task P3-08.1).

REQ-006.a-c, REQ-320.a, Tech Design DD-014, API §3.4. The key comes from ``X-Agentlens-Key`` or,
failing that, ``Authorization: Bearer`` (gRPC metadata is the same mapping with lowercase names).
Verification is parse → lookup by ``key_id`` → constant-time comparison of the salted hash.

A **positive** cache keeps a verified key for at most 5 s, which bounds revocation latency
(REQ-006.c); ``settings_changed`` clears it at once, in every process (DD-007). ``last_used_at``
is written at most once per minute per key. Failures carry a reason (missing, malformed, unknown,
revoked) for logs; the client always sees the same message (REQ-323).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.core.security import SecretHash, parse_api_key, verify_secret
from reflex_agentlens.ingest.errors import Unauthenticated
from reflex_agentlens.store.changefeed import ChangeFeed
from reflex_agentlens.store.records import ApiKeyRecord

__all__ = ["KEY_HEADER", "Authenticator", "KeyStore", "Principal", "extract_key"]

log = logging.getLogger(__name__)

KEY_HEADER = "x-agentlens-key"
_BEARER = "bearer "
_MAX_CACHE = 10_000


class KeyStore(Protocol):
    changes: ChangeFeed

    async def resolve_api_key(self, key_id: str) -> ApiKeyRecord | None: ...
    async def touch_api_key(self, key_id: str, at_ns: int) -> None: ...


@dataclass(frozen=True, slots=True)
class Principal:
    """Who sent the request: every span in it belongs to ``project_id`` (REQ-006.a)."""

    key_id: str
    project_id: str


def extract_key(headers: Mapping[str, str]) -> str | None:
    """``X-Agentlens-Key`` wins over ``Authorization: Bearer``; names are case-insensitive."""
    bearer: str | None = None
    for name, value in headers.items():
        lname = name.lower()
        if lname == KEY_HEADER:
            return value.strip()
        if lname == "authorization" and value[: len(_BEARER)].lower() == _BEARER:
            bearer = value[len(_BEARER) :].strip()
    return bearer


class Authenticator:
    def __init__(
        self,
        store: KeyStore,
        *,
        ttl_s: float = 5.0,
        touch_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        wall_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if ttl_s > 5.0:
            raise ValueError("the verification cache TTL must be at most 5 s (REQ-006.c)")
        self.store = store
        self.ttl_s = ttl_s
        self.touch_interval_s = touch_interval_s
        self.clock = clock
        self.wall_ns = wall_ns
        self._cache: dict[bytes, tuple[Principal, float]] = {}
        self._touched: dict[str, float] = {}

    def clear(self) -> None:
        self._cache.clear()

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        """The request's Principal, or :class:`Unauthenticated` with a reason."""
        plaintext = extract_key(headers)
        if not plaintext:
            raise Unauthenticated("missing")
        now = self.clock()
        fingerprint = hashlib.sha256(plaintext.encode()).digest()
        cached = self._cache.get(fingerprint)
        if cached is not None and cached[1] > now:
            principal = cached[0]
        else:
            principal = await self._verify(plaintext)
            self._remember(fingerprint, principal, now)
        await self._touch(principal.key_id, now)
        return principal

    async def _verify(self, plaintext: str) -> Principal:
        try:
            key_id, secret = parse_api_key(plaintext)
        except ValidationError:
            raise Unauthenticated("malformed") from None
        record = await self.store.resolve_api_key(key_id)
        if record is None or not verify_secret(secret, SecretHash(record.salt, record.secret_hash)):
            raise Unauthenticated("unknown")
        if record.revoked_at_ns is not None:
            raise Unauthenticated("revoked")
        return Principal(record.key_id, record.project_id)

    def _remember(self, fingerprint: bytes, principal: Principal, now: float) -> None:
        if len(self._cache) >= _MAX_CACHE:
            self._cache = {k: v for k, v in self._cache.items() if v[1] > now}
            if len(self._cache) >= _MAX_CACHE:
                self._cache.clear()
        self._cache[fingerprint] = (principal, now + self.ttl_s)

    async def _touch(self, key_id: str, now: float) -> None:
        last = self._touched.get(key_id)
        if last is not None and now - last < self.touch_interval_s:
            return
        self._touched[key_id] = now
        try:
            await self.store.touch_api_key(key_id, self.wall_ns())
        except Exception as exc:  # bookkeeping only: never fail the request on it
            log.warning("touch_api_key failed: %s", type(exc).__name__)

    async def watch(self) -> None:
        """REQ-006.c: any ``settings_changed`` (a revocation among them) clears the cache."""
        async with self.store.changes.subscribe() as events:
            async for event in events:
                if event.kind == "settings_changed":
                    self.clear()
