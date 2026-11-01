"""Safe Markdown for captured content (plan task P6-06.1; REQ-193, 327).

``react-markdown`` without ``rehype-raw`` (raw HTML is skipped, never parsed), without math
plugins, with the :data:`views.richtext.URL_TRANSFORM_JS` URL policy, links opened with
``rel="noopener noreferrer nofollow"``, and ``img`` mapped to a link (no remote fetch). Every
text node is escaped by React.
"""

from __future__ import annotations

from typing import Any

import reflex as rx
from reflex.vars.base import Var

from reflex_agentlens.views.richtext import URL_TRANSFORM_JS

__all__ = ["rich_text"]

REL = "noopener noreferrer nofollow"
# react-markdown passes each element's attributes as ``props`` to the mapped component
_HREF: Var[str] = Var(_js_expr="props.href", _var_type=str)
_SRC: Var[str] = Var(_js_expr="props.src", _var_type=str)
_ALT: Var[str] = Var(_js_expr="props.alt", _var_type=str)


def _link(*children: Any, **props: Any) -> rx.Component:
    return rx.el.a(*children, href=_HREF, rel=REL, target="_blank", **props)


def _image(*children: Any, **props: Any) -> rx.Component:
    """An image becomes a link to its (already filtered) source, or its alt text."""
    label = rx.cond(_ALT, _ALT, "image")
    return rx.el.span(
        rx.cond(
            _SRC,
            rx.el.a("[", label, "]", href=_SRC, rel=REL, target="_blank"),
            rx.fragment("[", label, "]"),
        ),
        class_name="agentlens-image-link",
    )


def rich_text(text: Var[str] | str) -> rx.Component:
    return rx.box(
        rx.markdown.root(
            text,
            component_map={"a": _link, "img": _image},
            remark_plugins=[rx.markdown.plugin.gfm],
            rehype_plugins=[],
            custom_attrs={
                "skipHtml": True,
                "urlTransform": Var(_js_expr=URL_TRANSFORM_JS),
            },
        ),
        class_name="agentlens-richtext",
    )
