"""Bundled pricing and cost per LLMCall (plan tasks P3-05.1, P3-05.2).

REQ-060.a/b, REQ-061.a-c, DD-016, Art. 3 (no runtime fetch), Art. 5 (integer nano-USD).
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import pytest

from reflex_agentlens.core.errors import Conflict, ValidationError
from reflex_agentlens.ingest.cost import (
    CSV_COLUMNS,
    BundledPricing,
    PricingSnapshot,
    bootstrap_pricing,
    load_bundled_pricing,
    parse_pricing_csv,
    price_call,
)
from reflex_agentlens.store.records import (
    LLMCallRow,
    PricingEntry,
    PricingVersion,
    PricingVersionMeta,
)

ROOT = Path(__file__).resolve().parents[3]
HEADER = ",".join(CSV_COLUMNS)
B = 1_000_000_000  # nano-USD per USD


def version(n: int, effective: int, *entries: PricingEntry) -> PricingVersion:
    return PricingVersion(PricingVersionMeta(n, effective, "editor", None, 0, None), entries)


def entry(pattern: str, p_in: int, p_out: int, provider: str = "", **kw: Any) -> PricingEntry:
    kind: Literal["exact", "glob"] = "glob" if "*" in pattern else "exact"
    return PricingEntry(pattern, kind, p_in * B, p_out * B, provider, **kw)


def call(start: int = 100, **kw: Any) -> LLMCallRow:
    base: dict[str, Any] = {"operation": "chat", "unpriced": True, "input_tokens": 1_000_000,
                            "output_tokens": 1_000_000, "provider": "openai"}  # fmt: skip
    return LLMCallRow(start_ns=start, duration_ns=1, **(base | kw))


# ── price_call (P3-05.2) ──────────────────────────────────────────────────────────────────────


def test_response_model_before_request_model() -> None:
    snap = PricingSnapshot([version(1, 0, entry("gpt-4o", 2, 10), entry("gpt-4o-2024", 3, 12))])
    row = price_call(call(request_model="gpt-4o", response_model="gpt-4o-2024"), snap)
    assert (row.cost_nusd, row.unpriced, row.pricing_version) == (15 * B, False, 1)


def test_request_model_when_response_unpriced() -> None:
    snap = PricingSnapshot([version(1, 0, entry("gpt-4o", 2, 10))])
    row = price_call(call(request_model="gpt-4o", response_model="gpt-4o-unknown-build"), snap)
    assert row.cost_nusd == 12 * B


def test_exact_before_glob_and_longest_prefix() -> None:
    snap = PricingSnapshot([version(1, 0, entry("gpt-*", 1, 1), entry("gpt-4o*", 2, 2),
                                    entry("gpt-4o-mini", 5, 5))])  # fmt: skip
    assert price_call(call(response_model="gpt-4o-mini"), snap).cost_nusd == 10 * B
    assert price_call(call(response_model="gpt-4o-x"), snap).cost_nusd == 4 * B
    assert price_call(call(response_model="gpt-3"), snap).cost_nusd == 2 * B


def test_provider_specific_beats_any_provider() -> None:
    snap = PricingSnapshot([version(1, 0, entry("m", 1, 1), entry("m", 7, 7, provider="openai"))])
    assert price_call(call(response_model="m"), snap).cost_nusd == 14 * B
    other = call(response_model="m", provider="anthropic")
    assert price_call(other, snap).cost_nusd == 2 * B


def test_version_in_effect_at_the_call_start() -> None:
    """REQ-061.a: max(version) with effective_from <= start, not simply the newest."""
    snap = PricingSnapshot([version(1, 0, entry("m", 1, 1)), version(2, 500, entry("m", 9, 9))])
    old, new = call(start=499, response_model="m"), call(start=500, response_model="m")
    assert (price_call(old, snap).cost_nusd, price_call(old, snap).pricing_version) == (2 * B, 1)
    assert (price_call(new, snap).cost_nusd, price_call(new, snap).pricing_version) == (18 * B, 2)
    assert price_call(call(start=-1, response_model="m"), snap).unpriced


@pytest.mark.parametrize(
    "row",
    [
        call(response_model="unknown-model"),
        call(response_model="m", input_tokens=None),
        call(response_model="m", output_tokens=None),
        call(),  # no model at all
    ],
)
def test_unpriced_calls(row: LLMCallRow) -> None:
    snap = PricingSnapshot([version(1, 0, entry("m", 1, 1))])
    out = price_call(row, snap)
    assert (out.cost_nusd, out.unpriced, out.pricing_version) == (None, True, None)


def test_empty_snapshot_is_unpriced() -> None:
    assert price_call(call(response_model="m"), PricingSnapshot([])).unpriced


def test_integer_arithmetic_with_cache_prices() -> None:
    """Art. 5: nano-USD integers end to end, cached input charged at its own price."""
    priced = entry("m", 3, 15, cached_input_nusd_per_mtok=300_000_000,
                   cache_write_nusd_per_mtok=3_750_000_000)  # fmt: skip
    row = call(response_model="m", input_tokens=1_234, output_tokens=567,
               cached_input_tokens=1_000, cache_write_tokens=7)  # fmt: skip
    out = price_call(row, PricingSnapshot([version(1, 0, priced)]))
    assert isinstance(out.cost_nusd, int)
    # (234*3e9 + 1000*0.3e9 + 567*15e9 + 7*(3.75e9 - 3e9) ... per Mtok) computed by core.money
    from reflex_agentlens.core.pricing import llm_cost

    assert out.cost_nusd == llm_cost(priced, 1_234, 567, 1_000, 7)


# ── CSV and bundled seed (P3-05.1) ────────────────────────────────────────────────────────────


def test_parse_csv_decimals_to_nano_usd() -> None:
    text = (f"{HEADER}\nopenai,gpt-4o,2.5,10,1.25,,litellm@abc,2026-11-01\n"
            ",claude-*,3,15,0.3,3.75,litellm@abc,2026-11-01\n")  # fmt: skip
    parsed = parse_pricing_csv(text)
    first, second = parsed.entries
    assert first == PricingEntry("gpt-4o", "exact", 2_500_000_000, 10_000_000_000, "openai",
                                 1_250_000_000, None)  # fmt: skip
    assert (second.match_kind, second.provider, second.cache_write_nusd_per_mtok) == (
        "glob", "", 3_750_000_000
    )  # fmt: skip
    assert parsed.note == "snapshot 2026-11-01; source: litellm@abc"


def test_sub_nano_prices_round_half_even() -> None:
    text = f"{HEADER}\n,m,0.0000000005,0.0000000015,,,s,d\n"
    parsed = parse_pricing_csv(text)
    assert (parsed.entries[0].input_nusd_per_mtok, parsed.entries[0].output_nusd_per_mtok) == (0, 2)


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ("a,b\n", "pricing.csv"),
        (f"{HEADER}\n,,1,1,,,s,d\n", "pricing.csv:2"),
        (f"{HEADER}\n,m,abc,1,,,s,d\n", "pricing.csv:2:input_per_mtok"),
        (f"{HEADER}\n,m,1,,,,s,d\n", "pricing.csv:2:output_per_mtok"),
        (f"{HEADER}\n,m,-1,1,,,s,d\n", "pricing.csv:2:input_per_mtok"),
        (f"{HEADER}\n,m,1,1,NaN,,s,d\n", "pricing.csv:2:cached_input_per_mtok"),
        (f"{HEADER}\n,m,1,1,,x,s,d\n", "pricing.csv:2:cache_write_per_mtok"),
    ],
)
def test_parse_csv_errors_name_the_line_and_column(body: str, where: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_pricing_csv(body)
    assert exc.value.field == where


def test_bundled_csv_loads_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Art. 3: loading prices never opens a socket."""

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access at runtime")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    seed = load_bundled_pricing()
    assert len(seed.entries) > 100
    assert all(type(e.input_nusd_per_mtok) is int for e in seed.entries)
    assert min(e.input_nusd_per_mtok for e in seed.entries) >= 0
    assert seed.note.startswith("snapshot ")
    assert "litellm@" in seed.note
    keys = [(e.provider, e.model_pattern) for e in seed.entries]
    assert len(keys) == len(set(keys))  # uq_pricing_entry holds
    gpt = next(e for e in seed.entries if (e.provider, e.model_pattern) == ("openai", "gpt-4o"))
    assert gpt.input_nusd_per_mtok > 0


def test_pricing_file_overrides_the_bundled_one(tmp_path: Path) -> None:
    path = tmp_path / "prices.csv"
    path.write_text(f"{HEADER}\n,local-model,1,2,,,mine,2026-10-31\n", encoding="utf-8")
    seed = load_bundled_pricing(path)
    assert [e.model_pattern for e in seed.entries] == ["local-model"]


class FakeStore:
    def __init__(self, versions: int = 0, conflict: bool = False) -> None:
        self.versions = versions
        self.conflict = conflict
        self.saved: list[dict[str, Any]] = []

    async def list_pricing_versions(self) -> Sequence[object]:
        return [object()] * self.versions

    async def save_pricing(
        self, entries: Sequence[PricingEntry], author: str | None, *,
        effective_from_ns: int | None = None, source: Any = "editor", note: str | None = None,
    ) -> PricingVersion:  # fmt: skip
        if self.conflict:
            raise Conflict("pricing version")
        self.saved.append({"entries": entries, "author": author, "effective": effective_from_ns,
                           "source": source, "note": note})  # fmt: skip
        self.versions += 1
        return version(self.versions, 0)


async def test_bootstrap_is_idempotent() -> None:
    seed = BundledPricing((entry("m", 1, 1),), "snapshot x; source: y")
    store = FakeStore()
    assert await bootstrap_pricing(store, seed)
    assert not await bootstrap_pricing(store, seed)
    assert store.saved == [{"entries": seed.entries, "author": None, "effective": 0,
                            "source": "bundled", "note": "snapshot x; source: y"}]  # fmt: skip


async def test_bootstrap_loses_the_race_quietly() -> None:
    assert not await bootstrap_pricing(FakeStore(conflict=True), BundledPricing((), "n"))


# ── dev-time generator (scripts/update_pricing.py) ────────────────────────────────────────────


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "update_pricing", ROOT / "scripts/update_pricing.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def price(provider: str, inp: str, out: str | None, cached: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"litellm_provider": provider, "mode": "chat",
                            "input_cost_per_token": Decimal(inp)}  # fmt: skip
    if out is not None:
        spec["output_cost_per_token"] = Decimal(out)
    if cached is not None:
        spec["cache_read_input_token_cost"] = Decimal(cached)
    return spec


def test_generator_transform_is_exact_and_deterministic() -> None:
    mod = _script()
    table = {
        "sample_spec": {"mode": "chat"},
        "gpt-4o": price("openai", "2.5e-06", "1e-05", "1.25e-06"),
        "gemini/gemini-x": price("gemini", "1e-06", "2e-06"),
        "gemini-x": price("vertex_ai-language-models", "1.5e-06", "2e-06"),
        "ft:gpt-4o:org": price("openai", "1e-06", "1e-06"),
        "bedrock/x": price("bedrock", "1e-06", "1e-06"),
        "dall-e": {"litellm_provider": "openai", "mode": "image_generation"},
        "no-out": price("openai", "1e-06", None),
        "neg": price("openai", "-1e-06", "1e-06"),
    }
    rows = mod.build_rows(table, "litellm@abc", "2026-11-01")
    got = [(r["provider"], r["model_pattern"], r["input_per_mtok"], r["output_per_mtok"],
            r["cached_input_per_mtok"]) for r in rows]  # fmt: skip
    assert got == [
        ("", "gpt-4o", "2.5", "10", "1.25"),  # unique model: also a provider-less row
        ("gcp.gemini", "gemini-x", "1", "2", ""),
        ("gcp.vertex_ai", "gemini-x", "1.5", "2", ""),  # prices differ: no '' row
        ("openai", "gpt-4o", "2.5", "10", "1.25"),
    ]
    text = mod.to_csv(rows)
    assert text.splitlines()[0] == HEADER
    assert parse_pricing_csv(text).entries[0].input_nusd_per_mtok == 2_500_000_000
    assert mod.build_rows(table, "litellm@abc", "2026-11-01") == rows


def test_generator_license_check() -> None:
    mod = _script()
    mod.check_license("MIT License\nPermission is hereby granted, free of charge, to any person")
    with pytest.raises(SystemExit):
        mod.check_license("Business Source License")
    with pytest.raises(SystemExit):
        mod._get("http://example.com")
