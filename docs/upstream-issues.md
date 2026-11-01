# Upstream issue drafts: silverpoint

Plan task P6-13.1 (Tech Design DD-017, PRD §4.1 goal). These are **drafts**. Filing them on
GitHub is the maintainer's decision, because it is outward-facing. Each draft states the gap that
agentlens hit, the workaround it ships in v0.1, and a proposed API in the style of the existing
catalog.

The gaps were checked against `reflex-silverpoint-react` 0.8.1 (the version agentlens pins) and
against the silverpoint 0.9.1 catalog on 2026-11-01. All four are still open in 0.9.1.

| # | Title | agentlens workaround in v0.1 | REQ |
|---|---|---|---|
| 1 | Multi-series line/area chart | One `stacked_bar_chart` per time bucket | 106.b, 107 |
| 2 | Box plot from precomputed quantiles | `dumbbell_chart` from p50 to p95 | 106.c |
| 3 | Time-scale x axis for series charts | Bucket labels formatted on the server as strings | 141.b |
| 4 | Trace waterfall chart | A span-tree table with proportional bars, built from Reflex primitives | 150 |

---

## 1. Multi-series line/area chart

**Title:** `line_chart` / `area_chart`: draw N series (one per key) instead of one plus a baseline

**Problem.** `line_chart` draws one series plus an optional dotted `secondaryKey` baseline.
`stream_chart` draws two waves. Plotting "tokens per hour by model", where there are usually 3 to
8 models, needs one line per model on a shared x axis. Charts with that shape are common in
observability: requests by status code, latency by endpoint, and cost by provider.

**What we do today.** agentlens uses a `stacked_bar_chart` per time bucket. This shows the totals,
but crossovers and the trend of each series are hard to read.

**Proposal.** A `keys` accessor list, like the one `stacked_bar_chart` already has, on
`line_chart` (and on `area_chart`, with an optional `stacked` flag):

```python
sp.line_chart(
    data=rows,  # [{"bucket": "10:00", "gpt-4o": 1200, "claude": 800}, …]
    x_key="bucket",
    keys=["gpt-4o", "claude", "o1"],  # one series each; colours from the palette's series ramp
    names={"o1": "o1 (reasoning)"},  # optional display names, as on stacked_bar_chart
    connect_nulls=False,
)
```

Wishes:
- A legend that can toggle series.
- A shared hover cursor that shows every series at that x.
- `on_select` that returns the series key and the x value.

---

## 2. Box plot from precomputed quantiles

**Title:** `box_plot_chart`: accept precomputed quartiles/whiskers per group

**Problem.** `box_plot_chart` takes raw observations (`valueKey`, `groupKey`) and computes the
quartiles in the browser. agentlens computes latency quantiles in SQL over millions of spans
(p50, p95, and the min/max or p5/p99 whiskers). Shipping the raw observations to the browser is
not an option. There is no way to pass the summary directly.

**What we do today.** A `dumbbell_chart` that goes from p50 to p95 for each operation. It loses
the box and the whiskers.

**Proposal.** A summary mode, selected by the presence of the quantile accessors:

```python
sp.box_plot_chart(
    data=[{"op": "chat", "p5": 120, "p25": 300, "p50": 410, "p75": 700, "p95": 1900}],
    group_key="op",
    low_key="p5",
    q1_key="p25",
    median_key="p50",
    q3_key="p75",
    high_key="p95",
    # outliers: not drawn in summary mode
)
```

`value_key` and `low_key`…`high_key` would be mutually exclusive. The data table alternative
(REQ-121 in silverpoint) would list the five numbers.

---

## 3. Time-scale x axis for series charts

**Title:** Time x axis (`axis="time"`) for `line_chart`, `area_chart`, `bar_chart`,
`stacked_bar_chart`

**Problem.** Series charts treat x as categorical strings. Time-series data has gaps (buckets
with no data), and the right density of tick labels depends on the time range (minutes, hours or
days). With categorical x, a missing bucket silently closes up the gap, and labels collide on long
ranges. `interval_chart` has `axis="date"`, but it only reads whole ISO days in UTC.

**What we do today.** The server fills empty buckets with zeros and formats every label itself
(`10:00`, `Mon 3`, and so on). This works, but each consumer has to do it.

**Proposal.**

```python
sp.line_chart(
    data=rows,  # x values: epoch milliseconds or ISO-8601 timestamps
    x_key="ts",
    axis="time",  # proportional spacing; ticks chosen for the domain
    time_zone="UTC",  # or an IANA name; labels via `locale`
    domain=(start_ms, end_ms),  # optional: show the requested range even when empty
)
```

On `interval_chart`, sub-day precision for `axis="date"` (or a new `axis="time"`) would cover the
same need.

---

## 4. Trace waterfall chart

**Title:** New chart: trace waterfall (nested spans on a shared time axis)

**Problem.** Distributed tracing UIs show a trace as a "waterfall". Each span is a row, indented
by its depth in the tree, with a bar placed at its start offset and sized by its duration. The
rows can be collapsed and selected. `interval_chart` is the closest match. Its lanes are flat,
though, and it does not support nesting, collapsing, row selection or thousands of rows. Its hand-
drawn style is also meant for a handful of intervals.

**What we do today.** A span-tree table built from Reflex primitives. Proportional bars are CSS
widths, and colours come from the silverpoint palette. It is not a silverpoint chart. Tech Design
DD-017 records this as a deliberate interpretation of Constitution Art. 7.

**Proposal.**

```python
sp.waterfall_chart(
    data=spans,  # [{"id", "parent", "name", "start_ms", "end_ms", "kind", "status"}]
    id_key="id",
    parent_key="parent",
    label_key="name",
    start_key="start_ms",
    end_key="end_ms",
    color_key="kind",  # mapped through the palette
    collapsed=[...],  # controlled list of collapsed ids
    selected="span-id",
    on_select=State.pick,  # returns the row id
    virtualize=True,  # thousands of rows
)
```

Accessibility: a tree-grid role, keyboard navigation (up/down, left/right to collapse), and the
existing data-table alternative.

---

## Filing checklist (maintainer)

- [ ] Search open issues first and comment on an existing issue instead of opening a duplicate.
- [ ] Attach a screenshot of the agentlens workaround.
- [ ] Link the filed issues from Tech Design DD-017 through a Delta Spec, then tick the PRD §4.1
      goal.
