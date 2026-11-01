"""Regenerate ``src/reflex_agentlens/data/pricing.csv`` from LiteLLM (plan task P3-05.1, DD-016).

Dev-time only: this is the one place network access is allowed (Art. 3; the server never fetches
prices). It pins the LiteLLM commit, checks that the price table is outside ``enterprise/`` and
that the repository license still grants it under MIT, then writes the DD-016 CSV.

    uv run python scripts/update_pricing.py            # latest main, pinned to its commit
    uv run python scripts/update_pricing.py --ref <sha>

Prices are parsed as ``Decimal`` from the JSON text (never as floats) and written as USD per
million tokens. Only first-party providers are kept, mapped to OTel ``gen_ai.provider.name``
values; a model name that is unique across them also gets a provider-less row (``''``), so
calls from other routes (Azure, Bedrock, proxies) reporting the same model still get priced.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = "BerriAI/litellm"
TABLE = "model_prices_and_context_window.json"
OUT = Path(__file__).resolve().parents[1] / "src/reflex_agentlens/data/pricing.csv"
COLUMNS = (
    "provider", "model_pattern", "input_per_mtok", "output_per_mtok", "cached_input_per_mtok",
    "cache_write_per_mtok", "source", "snapshot_date",
)  # fmt: skip
# LiteLLM provider → (OTel gen_ai.provider.name, model-key prefixes to strip)
PROVIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "openai": ("openai", ("openai/",)),
    "text-completion-openai": ("openai", ("openai/",)),
    "anthropic": ("anthropic", ("anthropic/",)),
    "gemini": ("gcp.gemini", ("gemini/",)),
    "vertex_ai-language-models": ("gcp.vertex_ai", ("vertex_ai/",)),
    "mistral": ("mistral_ai", ("mistral/",)),
    "cohere_chat": ("cohere", ("cohere_chat/", "cohere/")),
    "cohere": ("cohere", ("cohere/",)),
    "deepseek": ("deepseek", ("deepseek/",)),
    "xai": ("x_ai", ("xai/",)),
    "groq": ("groq", ("groq/",)),
    "perplexity": ("perplexity", ("perplexity/",)),
}
MODES = frozenset({"chat", "completion", "embedding", "responses"})
_MTOK = Decimal(1_000_000)


def _per_mtok(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"bad price {value!r}")
    text = format((value * _MTOK).normalize(), "f")
    return text


def build_rows(prices: dict[str, Any], source: str, snapshot_date: str) -> list[dict[str, str]]:
    """Pure transform of the LiteLLM table into CSV rows (sorted, deterministic)."""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for key, spec in prices.items():
        if not isinstance(spec, dict) or spec.get("mode") not in MODES:
            continue
        mapped = PROVIDERS.get(str(spec.get("litellm_provider")))
        if mapped is None:
            continue
        provider, prefixes = mapped
        model = key
        for prefix in prefixes:
            if model.startswith(prefix):
                model = model[len(prefix) :]
                break
        if not model or "/" in model or ":" in model or "*" in model or len(model) > 200:
            continue  # fine-tunes, regional routes, odd keys
        try:
            price_in = _per_mtok(spec.get("input_cost_per_token"))
            price_out = _per_mtok(spec.get("output_cost_per_token"))
            cached = _per_mtok(spec.get("cache_read_input_token_cost"))
            write = _per_mtok(spec.get("cache_creation_input_token_cost"))
        except ValueError:
            continue
        if price_in is None or price_out is None:
            continue
        rows.setdefault(
            (provider, model),
            {
                "provider": provider, "model_pattern": model, "input_per_mtok": price_in,
                "output_per_mtok": price_out, "cached_input_per_mtok": cached or "",
                "cache_write_per_mtok": write or "", "source": source,
                "snapshot_date": snapshot_date,
            },
        )  # fmt: skip
    by_model: dict[str, list[dict[str, str]]] = {}
    for (_, model), row in rows.items():
        by_model.setdefault(model, []).append(row)
    for model, same in by_model.items():
        prices_of = {tuple(r[c] for c in COLUMNS[2:6]) for r in same}
        if len(prices_of) == 1:  # every first-party route agrees: any provider may use it
            rows[("", model)] = same[0] | {"provider": ""}
    return [rows[key] for key in sorted(rows)]


def to_csv(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def check_license(text: str) -> None:
    """The table must stay MIT: outside ``enterprise/`` and under the repository's MIT grant."""
    if "MIT License" not in text or "Permission is hereby granted, free of charge" not in text:
        raise SystemExit("LiteLLM LICENSE no longer grants MIT: review before updating prices")
    if TABLE.startswith("enterprise/"):
        raise SystemExit("the price table moved under enterprise/: not MIT")


def _get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SystemExit("refusing a non-HTTPS URL")
    headers = {"User-Agent": "reflex-agentlens-pricing"}
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 (https only, checked above)
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https only)
        return bytes(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ref", default="main", help="LiteLLM branch, tag or commit")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    commit = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{args.ref}"))
    sha, date = commit["sha"], commit["commit"]["committer"]["date"][:10]
    raw = f"https://raw.githubusercontent.com/{REPO}/{sha}"
    check_license(_get(f"{raw}/LICENSE").decode("utf-8"))
    prices = json.loads(_get(f"{raw}/{TABLE}"), parse_float=Decimal)
    rows = build_rows(prices, f"litellm@{sha[:12]}", date)
    args.out.write_text(to_csv(rows), encoding="utf-8")
    print(f"{len(rows)} entries from litellm@{sha[:12]} ({date}) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
