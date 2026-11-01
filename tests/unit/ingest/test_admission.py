"""Admission: API keys and the verification cache (plan task P3-08.1; REQ-006, REQ-320.a)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from reflex_agentlens.core.security import generate_api_key, hash_secret
from reflex_agentlens.ingest.admission import Authenticator, KeyStore, Principal, extract_key
from reflex_agentlens.ingest.errors import Unauthenticated
from reflex_agentlens.store.changefeed import ChangeFeed
from reflex_agentlens.store.records import ApiKeyRecord, ChangeEvent

KEY = generate_api_key()
OTHER = generate_api_key()


class FakeKeys:
    def __init__(self) -> None:
        self.changes = ChangeFeed()
        h = hash_secret(KEY.secret)
        self.records = {
            KEY.key_id: ApiKeyRecord(KEY.key_id, "p1", "ci", h.salt, h.digest, 1, None, None, None)
        }
        self.lookups = 0
        self.touches: list[tuple[str, int]] = []
        self.touch_fails = False

    async def resolve_api_key(self, key_id: str) -> ApiKeyRecord | None:
        self.lookups += 1
        return self.records.get(key_id)

    async def touch_api_key(self, key_id: str, at_ns: int) -> None:
        self.touches.append((key_id, at_ns))
        if self.touch_fails:
            raise OSError("db down")

    def revoke(self) -> None:
        rec = self.records[KEY.key_id]
        self.records[KEY.key_id] = ApiKeyRecord(
            rec.key_id, rec.project_id, rec.label, rec.salt, rec.secret_hash, 1, None, None, 99
        )


class Rig:
    def __init__(self) -> None:
        self.now = 0.0
        self.store = FakeKeys()
        self.auth = Authenticator(
            cast(KeyStore, self.store), clock=lambda: self.now, wall_ns=lambda: int(self.now * 1e9)
        )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Agentlens-Key": " a "}, "a"),
        ({"authorization": "Bearer b"}, "b"),
        ({"Authorization": "bearer c", "x-agentlens-key": "d"}, "d"),  # the key header wins
        ({"Authorization": "Basic e"}, None),
        ({}, None),
    ],
)
def test_extract_key(headers: dict[str, str], expected: str | None) -> None:
    assert extract_key(headers) == expected


async def test_valid_key_maps_to_its_project() -> None:
    """REQ-006.a."""
    rig = Rig()
    principal = await rig.auth.authenticate({"authorization": f"Bearer {KEY.plaintext}"})
    assert principal == Principal(KEY.key_id, "p1")


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({}, "missing"),
        ({"x-agentlens-key": ""}, "missing"),
        ({"x-agentlens-key": "al_nope"}, "malformed"),
        ({"x-agentlens-key": OTHER.plaintext}, "unknown"),
        ({"x-agentlens-key": f"al_{KEY.key_id}_{OTHER.secret}"}, "unknown"),  # wrong secret
    ],
)
async def test_failures_are_401_with_a_reason(headers: dict[str, str], reason: str) -> None:
    """REQ-006.b: the reason is internal; the client message is fixed."""
    with pytest.raises(Unauthenticated) as err:
        await Rig().auth.authenticate(headers)
    assert err.value.reason == reason
    assert err.value.http_status == 401
    assert err.value.message == "invalid api key"


async def test_revoked_key_is_rejected_within_the_cache_ttl() -> None:
    """REQ-006.c: at most 5 s with a fake clock."""
    rig = Rig()
    headers = {"x-agentlens-key": KEY.plaintext}
    await rig.auth.authenticate(headers)
    rig.store.revoke()
    rig.now = 4.9
    await rig.auth.authenticate(headers)  # still cached
    assert rig.store.lookups == 1
    rig.now = 5.0
    with pytest.raises(Unauthenticated) as err:
        await rig.auth.authenticate(headers)
    assert err.value.reason == "revoked"


async def test_settings_changed_clears_the_cache_immediately() -> None:
    rig = Rig()
    headers = {"x-agentlens-key": KEY.plaintext}
    await rig.auth.authenticate(headers)
    watcher = asyncio.create_task(rig.auth.watch())
    await asyncio.sleep(0)
    rig.store.revoke()
    await rig.store.changes.publish(ChangeEvent.build("settings_changed", "p1", [], 1))
    await asyncio.sleep(0.01)
    watcher.cancel()
    with pytest.raises(Unauthenticated):
        await rig.auth.authenticate(headers)


async def test_last_used_is_touched_at_most_once_per_minute() -> None:
    rig = Rig()
    headers = {"x-agentlens-key": KEY.plaintext}
    for t in (0.0, 30.0, 59.9, 60.0, 61.0, 125.0):
        rig.now = t
        await rig.auth.authenticate(headers)
    assert [at for _, at in rig.store.touches] == [0, 60 * 10**9, 125 * 10**9]


async def test_touch_failure_does_not_fail_the_request() -> None:
    rig = Rig()
    rig.store.touch_fails = True
    assert (await rig.auth.authenticate({"x-agentlens-key": KEY.plaintext})).project_id == "p1"


def test_cache_ttl_above_five_seconds_is_refused() -> None:
    with pytest.raises(ValueError, match="5 s"):
        Authenticator(cast(KeyStore, FakeKeys()), ttl_s=6)
