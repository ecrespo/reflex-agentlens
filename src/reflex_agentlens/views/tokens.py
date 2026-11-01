"""Colour tokens, without Reflex (plan task P6-01.1; REQ-130.e, 150.a, DD-017).

One silverpoint palette preset for every chart and component, plus the span-kind colours of
API §6.8 and the status colours. Every token is used as text (badges, labels) on the palette's
substrate, so each must reach WCAG AA (4.5:1) there; the test computes it with silverpoint's
own ``contrast_ratio``.
"""

from __future__ import annotations

__all__ = ["KIND_COLORS", "KIND_LABELS", "PALETTE", "ROOT_CLASS", "STATUS_COLORS", "STATUS_LABELS"]

PALETTE = "silverpoint"  # silverpoint preset name (PALETTES["silverpoint"])
ROOT_CLASS = "agentlens"  # every agentlens style is scoped below this class (REQ-190.d)

KIND_COLORS: dict[str, str] = {
    "llm": "#2F5D8A",
    "tool": "#6B4E8A",
    "agent": "#3D6B4F",
    "approval": "#8A5A1F",
    "generic": "#5A5E65",
}
KIND_LABELS: dict[str, str] = {
    "llm": "LLM",
    "tool": "Tool",
    "agent": "Agent",
    "approval": "Approval",
    "generic": "Span",
}

STATUS_COLORS: dict[str, str] = {
    "ok": "#2E6B3F",
    "error": "#9E2B25",
    "in_progress": "#2F5D8A",
    "incomplete": "#7A5A12",
    "unpriced": "#7A5A12",
}
STATUS_LABELS: dict[str, str] = {
    "ok": "OK",
    "error": "Error",
    "in_progress": "In progress",
    "incomplete": "Incomplete",
    "unpriced": "Unpriced",
}
