"""Runs page (frame only until its P6 task fills the body)."""

from __future__ import annotations

import reflex as rx

from reflex_agentlens.pages._base import AgentlensState, empty_state, page_frame

__all__ = ["ON_LOAD", "TITLE", "page"]

TITLE = "Runs"
ON_LOAD = [AgentlensState.check_auth]


def page() -> rx.Component:
    return page_frame(TITLE, empty_state())
