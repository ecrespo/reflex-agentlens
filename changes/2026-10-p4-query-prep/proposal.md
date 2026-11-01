# Proposal — Query prerequisites (Analyze A-05, A-10)

> Status: `APPROVED` under the maintainer's standing authorization of 2026-10-31 ("continue with
> all implementation phases without approval"). Folded into `specs/` in the same PR. The
> maintainer can revert any decision; each one is isolated in `delta-spec.md`.
> Base specs affected: API v1.3.1 → v1.4, Data Model v1.2 → v1.3 (§8 pointer only),
> Tasks v1.2 → v1.3 · Date: 2026-10-31 · Author: Claude (for Ernesto Crespo)

## Problem

The Analyze report lists two findings to resolve "before P4":

| Finding | Risk if left open |
|---|---|
| A-05 (HIGH) | API §6.1 defers field-level DTO types to the Data Model, which only maps DTOs to tables. Fourteen DTOs have no fields, and `RunSummary` cannot carry DD-012's `usage_source` footnote. P4 would invent the contract while coding it, and P6 pages would bind to it |
| A-10 | `spans.category` (6 values) and `graph_nodes.node_kind` (3 values) differ from API `SpanKind` (5) and `GraphNode.kind` (5). Without one mapping, `query` and the UI colors can disagree |

## Decisions (details in `delta-spec.md`)

| Finding | Decision | Alternatives rejected |
|---|---|---|
| A-05 | API §6.7 "DTO reference": every §6 DTO with every field and type, plus the input types (`RunRef`, `SourceRef`, `AggScope`, `SessionFilter`, …). `RunSummary` gains `usage_source`, `span_count`, `missing_parents`, `source_count` | Field tables in the Data Model (the API owns the contract; the Data Model keeps the table mapping) |
| A-05 | `RunFilter.source_ids` → `sources` holding Source labels (`name` or `ns/name`), as the URL already does | Source ULIDs in URLs (not human-readable, and the store keys Sources by name) |
| A-10 | API §6.8 mapping table: `generic_genai` → `generic` (with `SpanNode.is_genai`), `workflow` → `agent`; `tool`/`llm`/`approval` graph kinds come from the span category; `approval` overrides | Widening `SpanKind` to six values (the UI has five colors per REQ-150.a) |
| — | Cursor signing key: `QueryService` takes the key as a constructor argument (the runtime passes the session secret, P5) | Reading the secret inside `query` (I/O at construction, layering) |

## Out of scope

PRD requirements are unchanged. No schema change.
