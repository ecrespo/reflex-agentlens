# Delta — Query prerequisites (Analyze A-05, A-10)

Base: PRD v1.1, API v1.3.1, Tech Design v1.4, Data Model v1.2, Tasks v1.2.
Evidence: Analyze report `specs/analyze/agentlens-analyze-2026-10-31.md` ("Before P4: A-05, A-10").
Status: adopted under the maintainer's standing authorization of 2026-10-31 ("continue with all
implementation phases without approval"), folded into `specs/` in the same PR
(`spec/P4-query-prep`). The maintainer can revert any decision below; each one is isolated.

## D1 — DTO reference (A-05, HIGH)

### ADDED · API → §6.7 "DTO reference"
Every DTO of §6 with every field and type, plus the input types used by `QueryService` and
`AdminService`. The full text is folded verbatim into the API spec (§6.7). Highlights:

- `RunSummary` gains `usage_source` (DD-012 footnote), `total_tokens`, `last_span_ns`,
  `source_count`, `span_count` and `missing_parents`; `source` becomes `SourceRef | None`.
- New: `RunRef`, `SourceRef`, `TimeExpr`, `Range`, `AttrPredicate`, `SessionFilter`, `AggScope`,
  `Page`, `TokenCostTotals`, `MessagePart`, `ToolCallView`, `ApprovalView`, `ErrorInfo`,
  `SpanDetail`, `GraphNode`, `GraphEdge`, `SessionSummary`, `SessionRunView`, `SessionDetail`,
  `DashboardData` (with per-panel `failed`, REQ-130.d), `Kpis`, `TimeSeries`, `ModelUsage`,
  `LatencyRow`, `ToolStat`, `SourceUsage`, `ApprovalStats`, `ProjectRef`, `SourceView`,
  `ChangeNotice`, `StorageStatus`, `Project`, `ProjectSettingsPatch`, `ApiKeyView`,
  `CreatedApiKey` (plaintext as `SecretStr`), `PricingTable`, `PricingEntryView`,
  `PricingVersionInfo`, `UnpricedModel`, `PatternRule`, `FieldRule`, `SavedFilterView`.
- `Message.role` gains `"other"` for roles outside the four standard ones (no silent remap);
  `SpanNode` gains `is_genai`, `placeholder`, `duration_ns`, `source`, `agent_name`,
  `content_dropped`, `subtree_includes_unpriced`.

### MODIFIED · API → §6.1 table
- **Before**: `get_trace(viewer, run: RunRef)` → `TraceDetail`.
- **After**: `get_trace(viewer, run: RunRef, offset: int = 0, limit: int = 2000)` → `TraceDetail`
  (one page of the DFS order; totals and subtree totals always cover the whole trace).
- **ADDED** row: `get_span_detail(viewer, run: RunRef, span_id: str)` → `SpanDetail` (raises
  `NotFound`): the span drawer's single lazy call, with messages, tool I/O, error info and the raw
  view (REQ-151.a–e). `get_span_raw` stays.
- **ADDED** row: `get_project(viewer, project_id)` → `Project`; `list_projects(viewer)` →
  `list[ProjectRef]` limited to the viewer's allowed Projects.

### MODIFIED · API → §6.1 "Key DTOs"
- **After**: "Field-level definitions are in §6.7; the code block above is a summary." (The Data
  Model §8 keeps only the DTO ↔ table mapping.)

### MODIFIED · API → §6.2 `RunFilter`
- **Before**: `source_ids: list[str] = []`.
- **After**: `sources: list[str] = []  # Source labels ("name" or "ns/name"), §6.7`. The URL row
  `source` maps to `sources`. `project` accepts a slug or a Project ID.
- **ADDED**: "The cursor is signed with HMAC-SHA256 under a key passed to `QueryService` (the
  runtime passes the session secret, P5) and carries its sort; a cursor of another sort or with a
  bad signature raises `ValidationError(field="cursor")`."

### MODIFIED · API → §6.3 table
- **ADDED** row: `list_pricing_versions(viewer)` → `list[PricingVersionInfo]` (REQ-171.a history).
- **ADDED** row: `list_projects_admin(viewer)` → `list[Project]` (settings view).

### MODIFIED · Data Model → §8
- **After** (note under the table): "Field-level DTO definitions live in API §6.7."

## D2 — Kind mapping (A-10)

### ADDED · API → §6.8 "Kind mapping between storage and DTOs"
One table: `generic_genai` → `generic` with `is_genai = true`; `workflow` → `agent`;
`approval` span category overrides the graph node kind; `llm`/`tool` graph kinds only in fallback
sequences. Folded verbatim into the API spec.
