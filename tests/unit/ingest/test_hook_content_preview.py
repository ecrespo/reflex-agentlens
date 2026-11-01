"""Redaction hook, content drop and preview (plan task P3-04.2).

REQ-039.e, REQ-040.a/b, REQ-172.b.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from reflex_agentlens.core.errors import ConfigError, ValidationError
from reflex_agentlens.ingest.normalize.classify import classify_draft
from reflex_agentlens.ingest.normalize.content import drop_content, is_content_key
from reflex_agentlens.ingest.normalize.llm import llm_call
from reflex_agentlens.ingest.normalize.spans import FLAG_CONTENT_DROPPED, FLAG_REDACTED
from reflex_agentlens.ingest.normalize.tool import tool_call
from reflex_agentlens.ingest.redaction.engine import redact_draft
from reflex_agentlens.ingest.redaction.hook import HookFailed, load_hook, run_hook, span_view
from reflex_agentlens.ingest.redaction.preview import preview_redaction
from reflex_agentlens.ingest.redaction.rules import compile_rules
from reflex_agentlens.store.records import RedactionRuleSet
from tests.unit.ingest.drafts import event, mk

AWS = "AKIA" + "IOSFODNN7EXAMPLE"
RULES = RedactionRuleSet(
    presets={"secrets": True},
    pattern_rules=[{"name": "ticket", "regex": r"TCK-\d+"}],
    field_rules=[{"key_glob": "user.email", "action": "mask"}],
)

# ── hook (REQ-039.e) ──────────────────────────────────────────────────────────────────────────


def test_hook_loads_by_module_function_and_redacts() -> None:
    hook = load_hook("tests.unit.ingest.sample_hook:mask_user")
    out = run_hook(mk({"user.id": "u-42", "keep": 1}), hook)
    assert out.attributes == {"user.id": "[REDACTED:hook]", "keep": 1}
    assert out.flags & FLAG_REDACTED


def test_hook_returning_none_keeps_the_span() -> None:
    d = mk({"a": "b"})
    assert run_hook(d, load_hook("tests.unit.ingest.sample_hook:no_change")) is d
    assert run_hook(d, load_hook("tests.unit.ingest.sample_hook:Nested.hook")) is d


def test_hook_cannot_mutate_the_draft_it_was_given() -> None:
    d = mk({"user.id": "u-1"})
    run_hook(d, load_hook("tests.unit.ingest.sample_hook:mask_user"))
    assert d.attributes == {"user.id": "u-1"}


def test_hook_error_rejects_the_span_without_leaking_content() -> None:
    with pytest.raises(HookFailed) as exc:
        run_hook(mk({"a": "b"}), load_hook("tests.unit.ingest.sample_hook:boom"))
    assert exc.value.reason == "hook raised RuntimeError"
    assert "sk-abc" not in str(exc.value)


@pytest.mark.parametrize(
    "result",
    [
        5,
        {"name": 1},
        {"attributes": []},
        {"attributes": {"x": float("inf")}},
        {"attributes": {"x": object()}},
        {"events": [{"name": "e", "attributes": "nope"}]},
        {"links": "x"},
        {"status_message": 3},
    ],
)
def test_hook_invalid_results_reject_the_span(result: Any) -> None:
    def hook(view: dict[str, Any]) -> Any:
        return view | result if isinstance(result, dict) else result

    with pytest.raises(HookFailed):
        run_hook(mk({"a": "b"}), hook)


def test_hook_rejects_too_deep_values() -> None:
    deep: Any = "x"
    for _ in range(80):
        deep = [deep]

    def hook(view: dict[str, Any]) -> dict[str, Any]:
        view["attributes"]["deep"] = deep
        return view

    with pytest.raises(HookFailed):
        run_hook(mk(), hook)


@pytest.mark.parametrize(
    "spec",
    [
        "no_colon",
        ":fn",
        "mod:",
        "tests.unit.ingest.does_not_exist:fn",
        "tests.unit.ingest.sample_hook:missing",
        "tests.unit.ingest.sample_hook:NOT_CALLABLE",
    ],
)
def test_bad_hook_specs_are_config_errors(spec: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_hook(spec, source="env")
    assert exc.value.key == "ingest.redaction_hook"
    assert exc.value.source == "env"


# ── content drop (REQ-040.a) ──────────────────────────────────────────────────────────────────


def test_content_off_drops_content_keys_and_message_events() -> None:
    attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 10,
        "gen_ai.input.messages": "[]",
        "gen_ai.system_instructions": "be nice",
        "llm.input_messages.0.message.content": "hi",
        "input.value": "x",
    }
    events = [
        event("gen_ai.user.message", content="hello"),
        event("gen_ai.choice", finish_reason="stop"),
        event("exception", **{"exception.type": "E", "input.value": "leak"}),
    ]
    out = drop_content(mk(attrs, events=events))
    assert out.attributes == {
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 10,
    }
    assert [ev["name"] for ev in out.events] == ["exception"]
    assert out.events[0]["attributes"] == {"exception.type": "E"}
    assert out.dropped_keys == [
        "event:gen_ai.choice",
        "event:gen_ai.user.message",
        "gen_ai.input.messages",
        "gen_ai.system_instructions",
        "input.value",
        "llm.input_messages.0.message.content",
    ]
    assert out.flags & FLAG_CONTENT_DROPPED
    llm = llm_call(out, classify_draft(out))
    assert llm is not None
    assert llm.content_dropped
    assert llm.input_tokens == 10


def test_content_off_marks_tool_calls() -> None:
    d = mk({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "s",
            "gen_ai.tool.call.arguments": "{}", "gen_ai.tool.call.result": "ok"})  # fmt: skip
    out = drop_content(d)
    row = tool_call(out, classify_draft(out))
    assert row is not None
    assert row.content_dropped
    assert row.tool_name == "s"
    assert out.attributes == {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "s"}


def test_no_content_means_no_change() -> None:
    d = mk({"http.method": "GET"}, events=[event("exception")])
    assert drop_content(d) is d


@pytest.mark.parametrize(
    ("key", "hit"),
    [("gen_ai.prompt", True), ("gen_ai.prompt.0.content", True), ("llm.tools.0.tool.json_schema",
     True), ("gen_ai.request.model", False), ("llm.model_name", False)],
)  # fmt: skip
def test_is_content_key(key: str, hit: bool) -> None:
    assert is_content_key(key) is hit


# ── preview (REQ-172.b) ───────────────────────────────────────────────────────────────────────


def test_preview_text_sample() -> None:
    assert preview_redaction(RULES, f"key {AWS} ticket TCK-9") == (
        "key [REDACTED:secrets] ticket [REDACTED:ticket]"
    )


def test_preview_span_sample_equals_what_ingest_stores() -> None:
    attrs = {"user.email": "a@b.co", "prompt": f"use {AWS}", "n": 3}
    sample = {"name": "chat TCK-1", "attributes": attrs, "events": [event("e", note="TCK-2")]}
    shown = json.loads(preview_redaction(RULES, json.dumps(sample)))
    stored = span_view(redact_draft(mk(attrs, name="chat TCK-1", events=sample["events"]),
                                    compile_rules(RULES)))  # fmt: skip
    assert shown == {key: stored[key] for key in sample}
    assert shown["attributes"]["user.email"] == "[REDACTED:field]"


def test_preview_other_json_is_redacted_as_text() -> None:
    out = preview_redaction(RULES, json.dumps({"msg": f"k={AWS}", "other": 1}))
    assert json.loads(out) == {"msg": "k=[REDACTED:secrets]", "other": 1}


def test_preview_rejects_bad_input() -> None:
    with pytest.raises(ValidationError, match="64 KiB"):
        preview_redaction(RULES, "x" * (64 * 1024 + 1))
    with pytest.raises(ValidationError):
        preview_redaction(RedactionRuleSet({}, [{"regex": "("}], []), "x")
    with pytest.raises(ValidationError, match="not a span"):
        preview_redaction(RULES, json.dumps({"attributes": ["not", "an", "object"]}))


@pytest.mark.parametrize(
    "sample",
    ["{not json TCK-1", '["TCK-1"]', json.dumps({"name": "TCK-1", "extra": "TCK-2"})],
)
def test_preview_non_span_samples_fall_back_to_text(sample: str) -> None:
    out = preview_redaction(RULES, sample)
    assert "TCK-1" not in out
    assert "[REDACTED:ticket]" in out
