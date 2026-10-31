"""Built-in redaction presets (plan task P3-04.1; REQ-039.c, DD-013).

Each preset is one ``regex`` pattern (alternatives joined) plus an optional validator that must
accept the match (``credit_card`` uses Luhn so order numbers and timestamps survive). New
Projects enable ``secrets`` only (REQ-039.c).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["DEFAULT_PRESETS", "PRESETS", "Preset", "luhn_ok"]

_SECRETS = "|".join(
    (
        r"\bal_[0-9a-hjkmnp-tv-z]{8}_[0-9A-Za-z]{32}\b",  # agentlens keys (API §3.4)
        r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}",  # OpenAI / Anthropic style keys
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key IDs
        r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",  # GitHub tokens
        r"\bgithub_pat_[A-Za-z0-9_]{22,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",  # Slack
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT-shaped
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    )
)
# Linear time: one start per run (lookbehind) and possessive quantifiers, so a long near-miss
# such as ``aaaa…@a.a.a…`` costs one scan instead of one per offset.
_EMAIL = r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@(?:[A-Za-z0-9-]++\.)+[A-Za-z]{2,}+\b"
_PHONE = "|".join(
    (
        r"(?<![\w+])\+[1-9]\d{6,14}\b",  # E.164
        r"(?<![\w+])\+?\d{1,3}[ .-]\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}\b",  # +34 612 345 678
        r"(?<!\w)\(\d{3}\)[ .-]?\d{3}[ .-]\d{4}\b",  # (555) 123-4567
        r"(?<![\w.-])\d{3}[.-]\d{3}[.-]\d{4}\b",  # 555-123-4567
    )
)
_CARD = r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"


def luhn_ok(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    pattern: str
    validator: Callable[[str], bool] | None = None


PRESETS: dict[str, Preset] = {
    "secrets": Preset("secrets", _SECRETS),
    "email": Preset("email", _EMAIL),
    "phone": Preset("phone", _PHONE),
    "credit_card": Preset("credit_card", _CARD, luhn_ok),
}
DEFAULT_PRESETS: dict[str, bool] = {"secrets": True, "email": False, "phone": False,
                                    "credit_card": False}  # fmt: skip
