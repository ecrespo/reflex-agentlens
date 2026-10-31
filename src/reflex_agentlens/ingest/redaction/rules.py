"""Compile a Project's redaction rule set (plan task P3-04.1; REQ-039.b/d, DD-013).

Rules are compiled once per rule-set version and shared by every span of that Project. Save-time
validation (:func:`validate_rule_set`) compiles each pattern and runs it against a 64 KiB
adversarial sample under the per-value budget, so a catastrophic pattern is refused before it
reaches ingest (REQ-039.d). Limits: 50 pattern rules, 2 KiB per regex, 100 field rules
(Data Model §2.4).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import regex  # type: ignore[import-untyped]

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.ingest.redaction.presets import PRESETS
from reflex_agentlens.store.records import RedactionRuleSet

__all__ = [
    "MAX_FIELD_RULES",
    "MAX_GLOB_CHARS",
    "MAX_PATTERN_RULES",
    "MAX_REGEX_BYTES",
    "TIMEOUT_S",
    "CompiledRules",
    "FieldRule",
    "PatternRule",
    "compile_rules",
    "glob_match",
    "validate_rule_set",
]

TIMEOUT_S = 0.025  # per rule per value (API §3.5)
MAX_PATTERN_RULES, MAX_FIELD_RULES, MAX_REGEX_BYTES, MAX_GLOB_CHARS = 50, 100, 2048, 256
_SAMPLE = ("a" * 64 + "@" + "a." * 32 + "1-" * 64) * 128  # ~64 KiB of near-miss text


@dataclass(frozen=True, slots=True)
class PatternRule:
    name: str
    pattern: Any  # regex.Pattern[str]
    validator: Callable[[str], bool] | None = None
    index: int | None = None  # position in ``pattern_rules``; ``None`` for a built-in preset

    @property
    def marker(self) -> str:
        return f"[REDACTED:{self.name}]"


@dataclass(frozen=True, slots=True)
class FieldRule:
    key_glob: str
    action: Literal["drop", "mask"]

    def matches(self, key: str) -> bool:
        return glob_match(self.key_glob, key)


@dataclass(frozen=True, slots=True)
class CompiledRules:
    patterns: tuple[PatternRule, ...] = ()
    fields: tuple[FieldRule, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.patterns and not self.fields


def glob_match(glob: str, key: str) -> bool:
    """``fnmatch``-style ``*`` / ``?`` against the whole key, in O(len(glob) x len(key)).

    The classic two-pointer matcher: on a mismatch it only resumes after the most recent ``*``,
    so a glob such as ``*a*a*a*b`` can never backtrack exponentially (no regex involved).
    """
    g = k = 0
    star, mark = -1, 0
    while k < len(key):
        if g < len(glob) and (glob[g] == "?" or glob[g] == key[k]):
            g, k = g + 1, k + 1
        elif g < len(glob) and glob[g] == "*":
            star, mark = g, k
            g += 1
        elif star != -1:
            g, mark = star + 1, mark + 1
            k = mark
        else:
            return False
    while g < len(glob) and glob[g] == "*":
        g += 1
    return g == len(glob)


def _compile(source: str, where: str) -> Any:
    if len(source.encode()) > MAX_REGEX_BYTES:
        raise ValidationError(where, "regex is longer than 2 KiB")
    try:
        return regex.compile(source, regex.VERSION0)
    except (regex.error, ValueError, OverflowError) as exc:
        raise ValidationError(where, f"invalid regex ({type(exc).__name__})") from None


def compile_rules(rules: RedactionRuleSet) -> CompiledRules:
    """Enabled presets (in preset order), then enabled pattern rules, then field rules."""
    patterns: list[PatternRule] = []
    for name, preset in PRESETS.items():
        if rules.presets.get(name) is True:
            patterns.append(PatternRule(name, regex.compile(preset.pattern), preset.validator))
    if len(rules.pattern_rules) > MAX_PATTERN_RULES:
        raise ValidationError("pattern_rules", "at most 50 rules")
    for i, raw in enumerate(rules.pattern_rules):
        where = f"pattern_rules[{i}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("regex"), str):
            raise ValidationError(where, "needs a regex")
        source = str(raw["regex"])
        if raw.get("enabled", True) is False:
            continue
        given = raw.get("name")
        label = given if isinstance(given, str) and given else f"rule{i + 1}"
        patterns.append(PatternRule(label, _compile(source, where), index=i))
    if len(rules.field_rules) > MAX_FIELD_RULES:
        raise ValidationError("field_rules", "at most 100 rules")
    fields: list[FieldRule] = []
    for i, raw in enumerate(rules.field_rules):
        where = f"field_rules[{i}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("key_glob"), str):
            raise ValidationError(where, "needs a key_glob")
        glob = str(raw["key_glob"])
        if not glob or len(glob) > MAX_GLOB_CHARS:
            raise ValidationError(where, "key_glob must be 1-256 characters")
        action = raw.get("action", "mask")
        if action == "drop":
            fields.append(FieldRule(glob, "drop"))
        elif action == "mask":
            fields.append(FieldRule(glob, "mask"))
        else:
            raise ValidationError(where, "action must be drop or mask")
    return CompiledRules(tuple(patterns), tuple(fields))


def validate_rule_set(rules: RedactionRuleSet) -> CompiledRules:
    """Save-time check (REQ-039.d): compile, then run every pattern on the adversarial sample."""
    compiled = compile_rules(rules)
    for rule in compiled.patterns:
        if rule.index is None:  # built-in presets are linear-time by construction
            continue
        try:
            rule.pattern.sub("", _SAMPLE, timeout=TIMEOUT_S * 4)
        except TimeoutError:
            raise ValidationError(
                f"pattern_rules[{rule.index}]", "regex is too slow (timeout)"
            ) from None
    return compiled
