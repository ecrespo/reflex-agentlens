"""Span drawer (plan task P6-06.2; REQ-151, 152, 327).

Rendered from a ``views.detail.DrawerView`` held by the trace state: header, badges, LLM call
and usage, messages as role bubbles (text through :func:`rich_text`, the rest preformatted),
tool I/O as collapsible JSON, error and stack trace, approval, and the raw tab.
"""

from __future__ import annotations

from typing import Any

import reflex as rx

from reflex_agentlens.components.richtext import rich_text
from reflex_agentlens.components.theme import kind_badge

__all__ = ["drawer"]


def _pairs(items: Any) -> rx.Component:
    return rx.el.dl(
        rx.foreach(items, lambda p: rx.fragment(rx.el.dt(p.label), rx.el.dd(p.value))),
        class_name="agentlens-pairs",
    )


def _section(title: str, cond: Any, *children: rx.Component) -> rx.Component:
    return rx.cond(cond, rx.el.section(rx.el.h4(title), *children))


def _json(summary: str, body: Any) -> rx.Component:
    return rx.cond(
        body != "",
        rx.el.details(rx.el.summary(summary), rx.el.pre(rx.el.code(body)), open=True),
    )


def _part(part: Any) -> rx.Component:
    return rx.el.div(
        rx.cond(part.title != "", rx.el.strong(part.title)),
        rx.cond(
            part.markdown,
            rich_text(part.body),
            rx.el.details(rx.el.summary("Show"), rx.el.pre(rx.el.code(part.body)), open=True),
        ),
        class_name="agentlens-part",
    )


def _bubble(bubble: Any) -> rx.Component:
    return rx.el.div(
        rx.el.div(bubble.title, class_name="agentlens-bubble-title"),
        rx.foreach(bubble.parts, _part),
        class_name="agentlens-bubble agentlens-bubble-" + bubble.role,
        data_direction=bubble.direction,
    )


def detail_body(view: Any) -> rx.Component:
    return rx.fragment(
        _pairs(view.header),
        rx.cond(
            view.badges.length() > 0,
            rx.el.p(rx.foreach(view.badges, lambda b: rx.el.span(b, class_name="agentlens-flag"))),
        ),
        rx.cond(
            view.content_dropped,
            rx.el.p(
                "Content was not stored for this span (content storage is off or it was dropped).",
                class_name="agentlens-note",
            ),
        ),
        _section("LLM call", view.llm.length() > 0, _pairs(view.llm)),
        _section("Tokens and cost", view.usage.length() > 0, _pairs(view.usage)),
        _section("Messages", view.messages.length() > 0, rx.foreach(view.messages, _bubble)),
        _section(
            "Tool",
            view.tool.length() > 0,
            _pairs(view.tool),
            _json("Arguments", view.tool_arguments),
            _json("Result", view.tool_result),
        ),
        _section(
            "Error",
            (view.error.length() > 0) | (view.stacktrace != ""),
            _pairs(view.error),
            rx.cond(view.stacktrace != "", rx.el.pre(rx.el.code(view.stacktrace))),
        ),
        _section("Approval", view.approval.length() > 0, _pairs(view.approval)),
    )


def raw_body(sections: Any) -> rx.Component:
    return rx.foreach(
        sections,
        lambda s: rx.el.details(rx.el.summary(s.title), rx.el.pre(rx.el.code(s.body)), open=True),
    )


def drawer(view: Any, tab: Any, sections: Any, on_tab: Any, on_close: Any) -> rx.Component:
    """``view``: the DrawerView var; ``tab``: "detail" | "raw"; ``on_tab(name)``, ``on_close``."""
    return rx.el.aside(
        rx.el.header(
            kind_badge(view.kind),
            rx.el.h3(view.title),
            rx.el.button("Close", type="button", on_click=on_close, aria_label="Close span"),
        ),
        rx.el.div(
            rx.el.button(
                "Detail",
                type="button",
                role="tab",
                aria_selected=tab == "detail",
                on_click=on_tab("detail"),
            ),
            rx.el.button(
                "Raw attributes",
                type="button",
                role="tab",
                aria_selected=tab == "raw",
                on_click=on_tab("raw"),
            ),
            role="tablist",
        ),
        rx.cond(tab == "raw", raw_body(sections), detail_body(view)),
        class_name="agentlens-drawer",
        aria_label="Span detail",
    )
