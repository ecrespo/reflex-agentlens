"""Message and tool-I/O normalizer, shared by ``ingest`` and ``query`` (plan task P3-01.3).

REQ-034.a/b, Data Model N1, Tech Design §5.3 (input/output messages).

One LLM call's conversation arrives in one of several forms. Sources in priority order, the first
present wins (they are never merged):

1. attributes ``gen_ai.input.messages`` / ``gen_ai.output.messages``
   (+ ``gen_ai.system_instructions``);
2. deprecated per-message events ``gen_ai.{system,user,assistant,tool}.message`` and
   ``gen_ai.choice``;
3. the ``gen_ai.client.inference.operation.details`` event (attribute form inside an event);
4. OpenInference flattened ``llm.input_messages.*`` / ``llm.output_messages.*``;
5. legacy ``gen_ai.prompt`` / ``gen_ai.completion`` (JSON list, plain text, or ``.<i>.*`` lists).

All of them normalize to :class:`Message` lists. Part types ``text``, ``tool_call``,
``tool_call_response`` (→ ``tool_result``) and ``reasoning`` map 1:1; anything else becomes
``other`` with its raw JSON. Events are read through :class:`GenAIEvent`, which a log record can
also produce (``body`` carries the payload there), so OTLP logs can feed this later without a
schema change (REQ-034.b).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from reflex_agentlens.core.semconv import constants as k
from reflex_agentlens.core.semconv.mapping import flat

__all__ = ["Conversation", "GenAIEvent", "Message", "Part", "extract"]

PartType = Literal["text", "tool_call", "tool_result", "reasoning", "other"]


@dataclass(frozen=True, slots=True)
class Part:
    type: PartType
    content: str | None = None  # text, reasoning
    id: str | None = None  # tool call ID (tool_call, tool_result)
    name: str | None = None  # tool name (tool_call)
    arguments: Any = None  # tool_call arguments (JSON value)
    result: Any = None  # tool_result response (JSON value)
    raw_type: str | None = None  # other: the source part type
    raw_json: str | None = None  # other: the source part as JSON


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    parts: tuple[Part, ...]
    finish_reason: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    input: tuple[Message, ...] = ()
    output: tuple[Message, ...] = ()
    source: str = "none"  # which form was used (golden tests, debugging)


@dataclass(frozen=True, slots=True)
class GenAIEvent:
    """A span event today, an OTLP log record later (REQ-034.b)."""

    name: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None

    @property
    def payload(self) -> Mapping[str, Any]:
        body = _json(self.body)
        return body if isinstance(body, Mapping) else self.attributes


def _json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            try:
                return json.loads(stripped)
            except (ValueError, RecursionError):
                return value
    return value


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# ── form 1/3: GenAI parts ────────────────────────────────────────────────────────────────────


def _part(raw: Any) -> Part:
    if not isinstance(raw, Mapping):
        return Part("text", content=_text(raw))
    kind = raw.get("type")
    if kind == "text":
        return Part("text", content=_text(raw.get("content")))
    if kind == "reasoning":
        return Part("reasoning", content=_text(raw.get("content")))
    if kind == "tool_call":
        return Part(
            "tool_call",
            id=raw.get("id"),
            name=raw.get("name"),
            arguments=_json(raw.get("arguments")),
        )
    if kind == "tool_call_response":
        return Part("tool_result", id=raw.get("id"), result=_json(raw.get("response")))
    return Part("other", raw_type=str(kind), raw_json=_dumps(raw))


def _genai_messages(value: Any) -> list[Message]:
    items = _json(value)
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, Mapping):
            parts = item.get("parts")
            out.append(
                Message(
                    role=str(item.get("role", "user")),
                    parts=tuple(_part(p) for p in parts) if isinstance(parts, list) else (),
                    finish_reason=item.get("finish_reason"),
                    name=item.get("name"),
                )
            )
    return out


def _system(value: Any) -> list[Message]:
    parts = _json(value)
    if isinstance(parts, list) and parts:
        return [Message("system", tuple(_part(p) for p in parts))]
    if isinstance(parts, str) and parts:
        return [Message("system", (Part("text", content=parts),))]
    return []


def _attribute_form(attrs: Mapping[str, Any]) -> Conversation | None:
    inp, out = attrs.get(k.GEN_AI_INPUT_MESSAGES), attrs.get(k.GEN_AI_OUTPUT_MESSAGES)
    if inp is None and out is None:
        return None
    system = _system(attrs.get(k.GEN_AI_SYSTEM_INSTRUCTIONS))
    return Conversation(
        tuple(system + _genai_messages(inp)), tuple(_genai_messages(out)), "attributes"
    )


# ── form 2: deprecated per-message events ────────────────────────────────────────────────────

_EVENT_ROLES = {
    k.EVENT_SYSTEM_MESSAGE: "system",
    k.EVENT_USER_MESSAGE: "user",
    k.EVENT_ASSISTANT_MESSAGE: "assistant",
    k.EVENT_TOOL_MESSAGE: "tool",
}


def _tool_calls(raw: Any) -> list[Part]:
    calls = _json(raw)
    if not isinstance(calls, list):
        return []
    parts = []
    for call in calls:
        if isinstance(call, Mapping):
            fn = call.get("function")
            fn = fn if isinstance(fn, Mapping) else {}
            parts.append(
                Part(
                    "tool_call",
                    id=call.get("id"),
                    name=fn.get("name"),
                    arguments=_json(fn.get("arguments")),
                )
            )
    return parts


def _event_message(role: str, payload: Mapping[str, Any]) -> Message:
    content = payload.get("content")
    if role == "tool":
        return Message("tool", (Part("tool_result", id=payload.get("id"), result=_json(content)),))
    parts: list[Part] = []
    if content not in (None, ""):
        parts.append(Part("text", content=_text(content)))
    parts += _tool_calls(payload.get("tool_calls"))
    return Message(str(payload.get("role", role)), tuple(parts))


def _event_form(events: Sequence[GenAIEvent]) -> Conversation | None:
    inp: list[Message] = []
    choices: list[tuple[int, Message]] = []
    for ev in events:
        if ev.name in _EVENT_ROLES:
            inp.append(_event_message(_EVENT_ROLES[ev.name], ev.payload))
        elif ev.name == k.EVENT_CHOICE:
            payload = ev.payload
            message = _json(payload.get("message"))
            message = message if isinstance(message, Mapping) else {}
            msg = _event_message("assistant", message)
            index = payload.get("index", len(choices))
            finish = payload.get("finish_reason")
            choices.append(
                (
                    int(index) if isinstance(index, int) else len(choices),
                    Message(msg.role, msg.parts, finish_reason=finish),
                )
            )
    if not inp and not choices:
        return None
    return Conversation(
        tuple(inp), tuple(m for _, m in sorted(choices, key=lambda c: c[0])), "events"
    )


def _details_form(events: Sequence[GenAIEvent]) -> Conversation | None:
    for ev in events:
        if ev.name == k.EVENT_INFERENCE_DETAILS:
            found = _attribute_form(ev.payload)
            if found is not None:
                return Conversation(found.input, found.output, "details_event")
    return None


# ── form 4: OpenInference flattened lists ────────────────────────────────────────────────────


def _oi_message(item: Mapping[str, Any]) -> Message:
    parts: list[Part] = []
    role = str(item.get("message.role", "user"))
    if role == "tool":
        result = _json(item.get("message.content"))
        return Message(
            "tool", (Part("tool_result", id=item.get("message.tool_call_id"), result=result),)
        )
    content = item.get("message.content")
    if content not in (None, ""):
        parts.append(Part("text", content=_text(content)))
    for c in flat(item, "message.contents"):
        kind = c.get("message_content.type", "text")
        if kind == "text":
            parts.append(Part("text", content=_text(c.get("message_content.text"))))
        else:
            parts.append(Part("other", raw_type=str(kind), raw_json=_dumps(c)))
    for call in flat(item, "message.tool_calls"):
        parts.append(
            Part(
                "tool_call",
                id=call.get("tool_call.id"),
                name=call.get("tool_call.function.name"),
                arguments=_json(call.get("tool_call.function.arguments")),
            )
        )
    return Message(role, tuple(parts), name=item.get("message.name"))


def _openinference_form(attrs: Mapping[str, Any]) -> Conversation | None:
    inp, out = flat(attrs, k.OI_INPUT_MESSAGES), flat(attrs, k.OI_OUTPUT_MESSAGES)
    if not inp and not out:
        return None
    return Conversation(
        tuple(_oi_message(m) for m in inp), tuple(_oi_message(m) for m in out), "openinference"
    )


# ── form 5: legacy gen_ai.prompt / gen_ai.completion ─────────────────────────────────────────


def _legacy(attrs: Mapping[str, Any], key: str, default_role: str) -> list[Message]:
    value = _json(attrs.get(key))
    if isinstance(value, list):
        items: list[Any] = value
    elif isinstance(value, str) and value:
        items = [{"role": default_role, "content": value}]
    else:
        items = [
            {"role": i.get("role", default_role), "content": i.get("content")}
            for i in flat(attrs, key)
        ]
    out = []
    for item in items:
        if isinstance(item, Mapping):
            content = item.get("content")
            parts = (Part("text", content=_text(content)),) if content is not None else ()
            out.append(Message(str(item.get("role", default_role)), parts))
    return out


def _legacy_form(attrs: Mapping[str, Any]) -> Conversation | None:
    inp, out = (
        _legacy(attrs, k.GEN_AI_PROMPT, "user"),
        _legacy(attrs, k.GEN_AI_COMPLETION, "assistant"),
    )
    if not inp and not out:
        return None
    return Conversation(tuple(inp), tuple(out), "legacy")


def extract(attrs: Mapping[str, Any], events: Iterable[GenAIEvent] = ()) -> Conversation:
    """The conversation of one LLM call; ``Conversation()`` when it carries none."""
    evs = list(events)
    for found in (
        _attribute_form(attrs),
        _event_form(evs),
        _details_form(evs),
        _openinference_form(attrs),
        _legacy_form(attrs),
    ):
        if found is not None:
            return found
    return Conversation()
