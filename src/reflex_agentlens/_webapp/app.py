"""Spike S1a page: silverpoint chart, Mermaid graph and Pydantic DTOs held in state.

Spike code (Tasks P0-02.1). Not part of the shipped package; replaced in P6.
"""

from __future__ import annotations

import reflex as rx
import reflex_mermaidjs as rmj
import reflex_silverpoint_react as sp
from pydantic import BaseModel


class RunRow(BaseModel):
    trace_id: str
    agent: str
    tokens: int
    status: str


GRAPH = """flowchart LR
  planner[planner] --> search[search_web]
  search --> writer[writer]
  classDef taken stroke-width:3px
  class planner,search,writer taken
"""


class SpikeState(rx.State):
    runs: list[RunRow] = [
        RunRow(trace_id="4bf9a1", agent="planner", tokens=18200, status="ok"),
        RunRow(trace_id="77c0e2", agent="writer", tokens=5400, status="error"),
    ]
    clicks: int = 0

    @rx.event
    def bump(self) -> None:
        self.clicks += 1
        self.runs = [
            *self.runs,
            RunRow(trace_id=f"new{self.clicks}", agent="x", tokens=1, status="ok"),
        ]

    @rx.var
    def chart_rows(self) -> list[dict[str, str | int]]:
        return [{"agent": r.agent, "tokens": r.tokens} for r in self.runs]


def index() -> rx.Component:
    return rx.vstack(
        rx.heading("agentlens spike S1", id="title"),
        sp.bar_chart(data=SpikeState.chart_rows, x_key="agent", value_key="tokens", height=240),
        rmj.mermaid(
            chart=GRAPH,
            security_level="strict",
            font_awesome=False,
            icon_packs=[],
            id="graph",
        ),
        rx.foreach(
            SpikeState.runs,
            lambda r: rx.text(r.trace_id, " ", r.agent, " ", r.tokens, class_name="run-row"),
        ),
        rx.button("add run", on_click=SpikeState.bump, id="bump"),
        rx.text("clicks: ", SpikeState.clicks, id="clicks"),
    )


app = rx.App()
app.add_page(index, route="/")
