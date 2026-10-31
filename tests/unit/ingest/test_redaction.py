"""Redaction presets, rules and engine (plan task P3-04.1).

REQ-039.a/b/c/d, DD-013, Analyze A-07. Secret-shaped fixtures are assembled from pieces so
secret scanners don't flag the test file.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from reflex_agentlens.core.errors import ValidationError
from reflex_agentlens.ingest.normalize.spans import (
    FLAG_REDACTED,
    FLAG_REDACTION_TIMEOUT,
    FLAG_TRUNCATED,
    SpanDraft,
    draft,
    truncate,
)
from reflex_agentlens.ingest.redaction.engine import (
    FIELD_MARKER,
    TIMEOUT_MARKER,
    redact_draft,
    redact_text,
)
from reflex_agentlens.ingest.redaction.presets import DEFAULT_PRESETS, PRESETS, luhn_ok
from reflex_agentlens.ingest.redaction.rules import (
    CompiledRules,
    compile_rules,
    validate_rule_set,
)
from reflex_agentlens.store.records import RedactionRuleSet
from tests.unit.ingest.otlp import NOW_NS, span

OPENAI = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a" * 36
SLACK = "xoxb-" + "1234567890-abcdef"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0In0." + "c2lnbmF0dXJlLXZhbHVl"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----"
AGENTLENS = "al_" + "01h2x3y4" + "_" + "A" * 32
VISA = "4111 1111 1111 1111"


def rules(*presets: str, patterns: object = (), fields: object = ()) -> CompiledRules:
    return compile_rules(
        RedactionRuleSet(
            presets=dict.fromkeys(presets, True),
            pattern_rules=list(patterns),  # type: ignore[call-overload]
            field_rules=list(fields),  # type: ignore[call-overload]
        )
    )


def make(**attrs: object) -> SpanDraft:
    scope = InstrumentationScope(name="t")
    return draft(span(**attrs), Resource(), scope, NOW_NS, "")  # type: ignore[arg-type]


# ── presets (REQ-039.c) ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("secret", [OPENAI, AWS, GITHUB, SLACK, JWT, PEM, AGENTLENS])
def test_secrets_preset_positive(secret: str) -> None:
    out = redact_text(f"key is {secret} ok", rules("secrets"))
    assert secret not in out
    assert "[REDACTED:secrets]" in out


@pytest.mark.parametrize(
    "text", ["sk-short", "AKIA1234", "the skeleton key", "eyJ alone", "ghp_tooShort"]
)
def test_secrets_preset_negative(text: str) -> None:
    assert redact_text(text, rules("secrets")) == text


def test_unterminated_private_key_is_redacted_to_the_end() -> None:
    text = "pem: -----BEGIN " + "PRIVATE KEY-----\nMIIEvQ..."
    assert redact_text(text, rules("secrets")) == "pem: [REDACTED:secrets]"


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("mail ana.perez+tag@example.co.uk now", True),
        ("user@sub.example.io", True),
        ("not an email: a@b", False),
        ("@handle", False),
    ],
)
def test_email_preset(text: str, hit: bool) -> None:
    out = redact_text(text, rules("email"))
    assert ("[REDACTED:email]" in out) is hit


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("call +34612345678", True),
        ("call +34 612 345 678", True),
        ("call (555) 123-4567", True),
        ("call 555-123-4567", True),
        ("version 1.2.3", False),
        ("order 20261031", False),
    ],
)
def test_phone_preset(text: str, hit: bool) -> None:
    out = redact_text(text, rules("phone"))
    assert ("[REDACTED:phone]" in out) is hit


def test_presets_are_linear_on_adversarial_input() -> None:
    """Built-in presets must never hit the per-value budget on large near-miss text."""
    from reflex_agentlens.ingest.redaction.rules import _SAMPLE

    worst = ("a" * 1000 + "@" + "a." * 5000 + "1") * 2
    for preset in PRESETS.values():
        compiled = compile_rules(
            RedactionRuleSet(presets={preset.name: True}, pattern_rules=[], field_rules=[])
        )
        for text in (_SAMPLE, worst):
            compiled.patterns[0].pattern.sub("", text, timeout=0.25)  # raises on a regression


def test_luhn() -> None:
    assert luhn_ok("4111111111111111")
    assert luhn_ok(VISA)
    assert not luhn_ok("4111111111111112")
    assert not luhn_ok("123")  # too short
    assert not luhn_ok("1" * 20)  # too long


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        (f"card {VISA} thanks", True),
        ("card 5500-0000-0000-0004", True),
        ("order 4111111111111112", False),  # fails Luhn: kept
        ("ts 1730332800000", False),  # 13 digits, fails Luhn
    ],
)
def test_credit_card_preset_requires_luhn(text: str, hit: bool) -> None:
    out = redact_text(text, rules("credit_card"))
    assert ("[REDACTED:credit_card]" in out) is hit
    if not hit:
        assert out == text


def test_default_presets_enable_secrets_only() -> None:
    assert DEFAULT_PRESETS == {"secrets": True, "email": False, "phone": False,
                               "credit_card": False}  # fmt: skip
    assert set(DEFAULT_PRESETS) == set(PRESETS)


def test_disabled_presets_and_unknown_names_are_ignored() -> None:
    compiled = compile_rules(
        RedactionRuleSet(presets={"email": False, "nope": True}, pattern_rules=[], field_rules=[])
    )
    assert compiled.empty


# ── pattern and field rules (REQ-039.b) ───────────────────────────────────────────────────────


def test_pattern_rule_uses_its_name_in_the_marker() -> None:
    r = rules(patterns=[{"name": "ticket", "regex": r"TCK-\d+", "enabled": True}])
    assert redact_text("see TCK-123 and TCK-9", r) == "see [REDACTED:ticket] and [REDACTED:ticket]"


def test_disabled_and_unnamed_pattern_rules() -> None:
    r = rules(patterns=[{"regex": "x", "enabled": False}, {"regex": "secret"}])
    assert redact_text("x secret", r) == "x [REDACTED:rule2]"


@pytest.mark.parametrize(
    ("patterns", "fields", "field"),
    [
        ([{"name": "x"}], [], "pattern_rules[0]"),
        (["nope"], [], "pattern_rules[0]"),
        ([{"regex": "("}], [], "pattern_rules[0]"),
        ([{"regex": "a" * 2049}], [], "pattern_rules[0]"),
        ([{"regex": "a"}] * 51, [], "pattern_rules"),
        ([], [{"action": "drop"}], "field_rules[0]"),
        ([], [{"key_glob": "a", "action": "hash"}], "field_rules[0]"),
        ([], [{"key_glob": "a"}] * 101, "field_rules"),
    ],
)
def test_invalid_rules_are_refused(patterns: object, fields: object, field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        rules(patterns=patterns, fields=fields)
    assert exc.value.field == field


def test_field_rules_drop_and_mask_on_flattened_keys() -> None:
    r = rules(
        fields=[
            {"key_glob": "http.request.header.*", "action": "drop"},
            {"key_glob": "user.?d", "action": "mask"},
            {"key_glob": "db.*"},  # default: mask
        ]
    )
    d = make(**{"http.request.header.authorization": "Bearer x", "user.id": "u1",
                "db.statement": "select 1", "keep": "me"})  # fmt: skip
    out = redact_draft(d, r)
    assert out.attributes == {"user.id": FIELD_MARKER, "db.statement": FIELD_MARKER, "keep": "me"}
    assert out.flags & FLAG_REDACTED


def test_field_glob_matches_the_whole_key() -> None:
    r = rules(fields=[{"key_glob": "auth", "action": "drop"}])
    out = redact_draft(make(auth="a", auth_mode=3), r)
    assert out.attributes == {"auth_mode": 3}


# ── engine (REQ-039.a/d) ──────────────────────────────────────────────────────────────────────


def test_every_string_leaf_is_covered() -> None:
    """Span, event (exception message/stacktrace), link and resource attributes, status."""
    sp = span(prompt=f"use {AWS}", nested=[f"{AWS}"], n=3)
    sp.status.message = f"failed with {AWS}"
    ev = sp.events.add(name="exception")
    ev.attributes.add(key="exception.message").value.string_value = f"bad {AWS}"
    ev.attributes.add(key="exception.stacktrace").value.string_value = f"at x\n  key={AWS}"
    ln = sp.links.add(trace_id=b"\x01" * 16, span_id=b"\x02" * 8)
    ln.attributes.add(key="why").value.string_value = AWS
    res = Resource()
    res.attributes.add(key="env.key").value.string_value = AWS
    d = draft(sp, res, InstrumentationScope(name="t"), NOW_NS, "")
    out = redact_draft(d, rules("secrets"))
    assert AWS not in json.dumps(
        [out.attributes, out.events, out.links, out.resource, out.status_message]
    )
    assert out.attributes["n"] == 3
    assert out.flags & FLAG_REDACTED
    assert not out.flags & FLAG_REDACTION_TIMEOUT


def test_json_string_attributes_stay_valid_json() -> None:
    messages = [{"role": "user", "parts": [{"type": "text", "content": f"my key {AWS}"}]}]
    d = make(**{"gen_ai.input.messages": json.dumps(messages)})
    out = redact_draft(d, rules("secrets"))
    parsed = json.loads(str(out.attributes["gen_ai.input.messages"]))
    assert parsed[0]["parts"][0]["content"] == "my key [REDACTED:secrets]"
    assert parsed[0]["role"] == "user"


def test_untouched_json_keeps_its_original_text() -> None:
    raw = '{"a":  1}'  # odd spacing must survive when nothing matched
    assert redact_text(raw, rules("secrets")) == raw
    assert redact_text("[not json", rules("secrets")) == "[not json"


def test_no_match_returns_the_same_draft_and_flags() -> None:
    d = make(text="hello")
    assert redact_draft(d, rules("secrets")) == d
    assert redact_draft(d, CompiledRules()) is d


def test_bytes_values_cannot_hide_text() -> None:
    """A kvlist shaped like a bytes value ({"$b64": ...}) is still scanned (security review)."""
    d = dataclasses.replace(make(), attributes={"b": {"$b64": AWS}, "ok": {"$b64": "AAAA"}})
    out = redact_draft(d, rules("secrets"))
    assert out.attributes == {"b": "[REDACTED:secrets]", "ok": {"$b64": "AAAA"}}


def test_catastrophic_pattern_times_out_and_fails_closed() -> None:
    """REQ-039.d: the whole value is replaced, the span is flagged, ingest continues."""
    r = rules("secrets", patterns=[{"name": "evil", "regex": "(a|aa)+$"}])
    d = make(slow="a" * 40 + "!", fine="b")
    out = redact_draft(d, r)
    assert out.attributes["slow"] == TIMEOUT_MARKER
    assert out.attributes["fine"] == "b"
    assert out.flags & FLAG_REDACTION_TIMEOUT
    assert out.flags & FLAG_REDACTED


def test_timeout_inside_a_json_document() -> None:
    r = rules(patterns=[{"name": "evil", "regex": "(a|aa)+$"}])
    doc = json.dumps({"x": "a" * 40 + "!", "y": "z"})
    out = redact_draft(make(doc=doc), r)
    assert json.loads(str(out.attributes["doc"])) == {"x": TIMEOUT_MARKER, "y": "z"}
    assert out.flags & FLAG_REDACTION_TIMEOUT


def test_save_time_validation_refuses_a_slow_pattern() -> None:
    bad = RedactionRuleSet(
        presets={"secrets": True},
        pattern_rules=[{"regex": "x", "enabled": False}, {"regex": "ok"}, {"regex": "(a|aa)+$"}],
        field_rules=[],
    )
    with pytest.raises(ValidationError, match="too slow") as exc:
        validate_rule_set(bad)
    assert exc.value.field == "pattern_rules[2]"
    good = RedactionRuleSet(
        presets={"secrets": True, "email": True, "phone": True, "credit_card": True},
        pattern_rules=[{"regex": r"TCK-\d+"}],
        field_rules=[],
    )
    assert len(validate_rule_set(good).patterns) == 5


def test_canary_secret_straddling_the_truncation_limit() -> None:
    """Analyze A-07: redaction runs before truncation, so no prefix of the secret survives."""
    limit = 64
    text = "x" * (limit - 11) + " " + OPENAI + " tail"
    d = make(prompt=text)
    unsafe = truncate(d, limit)  # wrong order: a prefix of the key leaks
    assert "sk-proj" in str(unsafe.attributes["prompt"])
    safe = truncate(redact_draft(d, rules("secrets")), limit)
    stored = str(safe.attributes["prompt"])
    assert "sk-" not in stored
    assert OPENAI[:6] not in stored
    assert safe.flags & FLAG_REDACTED
    assert safe.flags & FLAG_TRUNCATED
