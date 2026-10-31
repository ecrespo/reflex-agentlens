"""Messages normalizer (plan task P3-01.3; REQ-034.a/b, Data Model N1)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from reflex_agentlens.core.semconv.messages import (
    Conversation,
    GenAIEvent,
    Message,
    Part,
    extract,
)

# the conversation every form encodes
SYSTEM = Message("system", (Part("text", content="be brief"),))
USER = Message("user", (Part("text", content="weather in Paris?"),))
CALL = Message(
    "assistant", (Part("tool_call", id="c1", name="weather", arguments={"city": "Paris"}),)
)
RESULT = Message("tool", (Part("tool_result", id="c1", result={"temp": 21}),))
ANSWER = Message("assistant", (Part("text", content="21 °C"),), finish_reason="stop")
WANT_IN = (SYSTEM, USER, CALL, RESULT)
WANT_OUT = (ANSWER,)


def attribute_form() -> dict[str, Any]:
    return {
        "gen_ai.system_instructions": json.dumps([{"type": "text", "content": "be brief"}]),
        "gen_ai.input.messages": json.dumps(
            [
                {"role": "user", "parts": [{"type": "text", "content": "weather in Paris?"}]},
                {
                    "role": "assistant",
                    "parts": [
                        {
                            "type": "tool_call",
                            "id": "c1",
                            "name": "weather",
                            "arguments": {"city": "Paris"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "parts": [{"type": "tool_call_response", "id": "c1", "response": {"temp": 21}}],
                },
            ]
        ),
        "gen_ai.output.messages": json.dumps(
            [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "21 °C"}],
                    "finish_reason": "stop",
                }
            ]
        ),
    }


def event_form() -> list[GenAIEvent]:
    call = [{"id": "c1", "type": "function",
             "function": {"name": "weather", "arguments": '{"city": "Paris"}'}}]  # fmt: skip
    return [
        GenAIEvent("gen_ai.system.message", {"content": "be brief"}),
        GenAIEvent("gen_ai.user.message", {"content": "weather in Paris?"}),
        GenAIEvent("gen_ai.assistant.message", {"tool_calls": json.dumps(call)}),
        GenAIEvent("gen_ai.tool.message", {"id": "c1", "content": '{"temp": 21}'}),
        GenAIEvent(
            "gen_ai.choice",
            {
                "index": 0,
                "finish_reason": "stop",
                "message": json.dumps({"role": "assistant", "content": "21 °C"}),
            },
        ),
    ]


def openinference_form() -> dict[str, Any]:
    p = "llm.input_messages"
    return {
        f"{p}.0.message.role": "system",
        f"{p}.0.message.content": "be brief",
        f"{p}.1.message.role": "user",
        f"{p}.1.message.contents.0.message_content.type": "text",
        f"{p}.1.message.contents.0.message_content.text": "weather in Paris?",
        f"{p}.2.message.role": "assistant",
        f"{p}.2.message.tool_calls.0.tool_call.id": "c1",
        f"{p}.2.message.tool_calls.0.tool_call.function.name": "weather",
        f"{p}.2.message.tool_calls.0.tool_call.function.arguments": '{"city": "Paris"}',
        f"{p}.3.message.role": "tool",
        f"{p}.3.message.tool_call_id": "c1",
        f"{p}.3.message.content": '{"temp": 21}',
        "llm.output_messages.0.message.role": "assistant",
        "llm.output_messages.0.message.content": "21 °C",
    }


def test_attribute_form() -> None:
    got = extract(attribute_form())
    assert (got.input, got.output, got.source) == (WANT_IN, WANT_OUT, "attributes")


def test_event_form() -> None:
    """REQ-034.a: the deprecated event form normalizes to the same messages."""
    got = extract({}, event_form())
    assert (got.input, got.output, got.source) == (WANT_IN, WANT_OUT, "events")


def test_inference_details_event() -> None:
    got = extract({}, [GenAIEvent("gen_ai.client.inference.operation.details", attribute_form())])
    assert (got.input, got.output, got.source) == (WANT_IN, WANT_OUT, "details_event")


def test_openinference_form() -> None:
    got = extract(openinference_form())
    # OpenInference has no finish reason on output messages
    assert got.input == WANT_IN
    assert got.output == (Message("assistant", ANSWER.parts),)
    assert got.source == "openinference"


def test_log_record_body_feeds_the_same_interface() -> None:
    """REQ-034.b: a log record (payload in the body) goes through the same input interface."""
    records = [GenAIEvent(ev.name, {}, body=json.dumps(dict(ev.attributes))) for ev in event_form()]
    got = extract({}, records)
    assert (got.input, got.output) == (WANT_IN, WANT_OUT)


def test_priority_attributes_beat_events_and_openinference() -> None:
    attrs = attribute_form() | openinference_form()
    assert extract(attrs, event_form()).source == "attributes"
    assert extract(openinference_form(), event_form()).source == "events"


@pytest.mark.parametrize(
    ("attrs", "want_in", "want_out"),
    [
        ({"gen_ai.prompt": "hi", "gen_ai.completion": "hello"},
         (Message("user", (Part("text", content="hi"),)),),
         (Message("assistant", (Part("text", content="hello"),)),)),
        ({"gen_ai.prompt": json.dumps([{"role": "system", "content": "s"}])},
         (Message("system", (Part("text", content="s"),)),), ()),
        ({"gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "q",
          "gen_ai.completion.0.content": "a"},
         (Message("user", (Part("text", content="q"),)),),
         (Message("assistant", (Part("text", content="a"),)),)),
    ],
)  # fmt: skip
def test_legacy_forms(
    attrs: dict[str, Any], want_in: tuple[Message, ...], want_out: tuple[Message, ...]
) -> None:
    got = extract(attrs)
    assert (got.input, got.output, got.source) == (want_in, want_out, "legacy")


def test_unknown_parts_are_kept_as_other() -> None:
    blob = {"type": "blob", "modality": "image", "mime_type": "image/png", "content": "AAA"}
    attrs = {
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [blob, {"type": "reasoning", "content": "hmm"}, "bare"]}]
        )
    }
    parts = extract(attrs).input[0].parts
    assert parts[0].type == "other"
    assert parts[0].raw_type == "blob"
    assert json.loads(parts[0].raw_json or "") == blob
    assert parts[1] == Part("reasoning", content="hmm")
    assert parts[2] == Part("text", content="bare")


def test_openinference_non_text_content_is_other() -> None:
    attrs = {
        "llm.input_messages.0.message.role": "user",
        "llm.input_messages.0.message.contents.0.message_content.type": "image",
        "llm.input_messages.0.message.contents.0.message_content.image.image.url": "u",
    }
    part = extract(attrs).input[0].parts[0]
    assert (part.type, part.raw_type) == ("other", "image")


def test_nothing_to_extract() -> None:
    assert extract({"http.method": "GET"}) == Conversation()
    assert extract({"gen_ai.input.messages": "not json"}) == Conversation((), (), "attributes")
    assert extract({}, [GenAIEvent("exception", {"exception.type": "E"})]) == Conversation()


def test_malformed_values_degrade_quietly() -> None:
    deep = "[" * 100_000 + "]" * 100_000
    attrs = {"gen_ai.input.messages": deep, "gen_ai.output.messages": json.dumps([1, {"role": 2}])}
    got = extract(attrs)
    assert got.input == ()
    assert got.output == (Message("2", ()),)
    events = [GenAIEvent("gen_ai.choice", {"message": "{bad", "index": "x"})]
    assert extract({}, events).output == (Message("assistant", ()),)
