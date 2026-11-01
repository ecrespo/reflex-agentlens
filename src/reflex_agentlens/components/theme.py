"""Theme root and badges (plan task P6-01.1; REQ-130.e, 190.d, DD-017).

``root`` wraps a page in the silverpoint provider with the one palette and the ``.agentlens``
scope class; no component sets global CSS (REQ-190.d).
"""

from __future__ import annotations

import reflex as rx
import reflex_silverpoint_react as sp  # type: ignore[import-untyped]

from reflex_agentlens.views.tokens import (
    KIND_COLORS,
    KIND_LABELS,
    PALETTE,
    ROOT_CLASS,
    STATUS_COLORS,
    STATUS_LABELS,
)

__all__ = ["kind_badge", "root", "status_badge"]


def root(*children: rx.Component, **props: object) -> rx.Component:
    return sp.silverpoint_provider(
        rx.box(*children, class_name=ROOT_CLASS, width="100%", **props),
        palette=PALETTE,
    )


def _badge(text: rx.Var[str] | str, color: rx.Var[str] | str) -> rx.Component:
    return rx.el.span(
        text,
        style={
            "color": color,
            "border": "1px solid currentColor",
            "border_radius": "4px",
            "padding": "0 6px",
            "font_size": "12px",
            "white_space": "nowrap",
        },
    )


def kind_badge(kind: rx.Var[str] | str) -> rx.Component:
    if isinstance(kind, str):
        return _badge(KIND_LABELS[kind], KIND_COLORS[kind])
    return _badge(rx.Var.create(KIND_LABELS)[kind], rx.Var.create(KIND_COLORS)[kind])


def status_badge(status: rx.Var[str] | str) -> rx.Component:
    if isinstance(status, str):
        return _badge(STATUS_LABELS[status], STATUS_COLORS[status])
    return _badge(rx.Var.create(STATUS_LABELS)[status], rx.Var.create(STATUS_COLORS)[status])
