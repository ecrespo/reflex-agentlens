"""Display formatting shared by the view models (P6). Pure functions, UTC, no locale."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

__all__ = ["DASH", "fmt_cost", "fmt_duration", "fmt_int", "fmt_time"]

DASH = "—"
NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def fmt_time(ns: int | None) -> str:
    """``2026-10-30 12:00:00`` (UTC)."""
    if ns is None:
        return DASH
    return datetime.fromtimestamp(ns / NS_PER_S, UTC).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(ns: int | None) -> str:
    """``850 ms``, ``1.23 s``, ``2 m 05 s``, ``1 h 02 m``; ``—`` when unknown."""
    if ns is None or ns < 0:
        return DASH
    if ns < NS_PER_MS:
        return f"{ns / 1000:.0f} µs" if ns >= 1000 else f"{ns} ns"
    if ns < NS_PER_S:
        return f"{ns / NS_PER_MS:.0f} ms"
    seconds = ns / NS_PER_S
    if seconds < 60:
        return f"{seconds:.2f} s"
    whole = int(seconds)
    if whole < 3600:
        return f"{whole // 60} m {whole % 60:02d} s"
    return f"{whole // 3600} h {whole % 3600 // 60:02d} m"


def fmt_int(n: int | None) -> str:
    return DASH if n is None else f"{n:,}"


def fmt_cost(cost: Decimal | None, unpriced: bool = False) -> str:
    """``$0.0123``; ``unpriced`` when nothing is priced; ``$0.01 + unpriced`` when partly."""
    if cost is None:
        return "unpriced" if unpriced else DASH
    if cost == 0 and unpriced:
        return "unpriced"
    places = 4 if abs(cost) < 1 else 2
    text = f"${cost:,.{places}f}"
    return f"{text} + unpriced" if unpriced else text
