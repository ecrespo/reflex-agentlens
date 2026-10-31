# Proposal — Ingest prerequisites (Analyze A-04, A-07, A-08, A-17)

> Status: `APPROVED` under the maintainer's standing authorization of 2026-10-31 ("continue with
> all implementation phases without approval"). Folded into `specs/` in the same PR. The
> maintainer can revert any decision; each one is isolated in `delta-spec.md`.
> Base specs affected: API v1.2 → v1.3, Tech Design v1.3 → v1.4, Data Model v1.1 → v1.2,
> Tasks v1.1 → v1.2 · Date: 2026-10-31 · Author: Claude (for Ernesto Crespo)

## Problem

The Analyze report lists four findings to resolve "before P3 starts":

| Finding | Risk if left open |
|---|---|
| A-04 (HIGH) | One bad batch (e.g. a Project deleted while its key is still cached) aborts a whole commit group of up to 5,000 spans from many clients |
| A-07 | Truncating before redacting can store the prefix of a secret that straddles the size limit (Art. 3) |
| A-08 | Redacted conversation IDs (e.g. emails) collapse unrelated conversations into one Session |
| A-17 | Future timestamps keep Runs `in_progress` and escape retention; very old spans are purged on arrival |

## Decisions (details in `delta-spec.md`)

| Finding | Decision | Alternatives rejected |
|---|---|---|
| A-04 | One `SAVEPOINT` per batch; data errors reject only that batch (400 / `INVALID_ARGUMENT`), other errors fail the group (503) | Per-batch transactions (SQLite commit-rate ceiling, REQ-300); re-validating every FK before writing (races remain) |
| A-07 | Redact, then truncate | Truncate with overlap (still leaks on long secrets) |
| A-08 | `redacted~HMAC16` natural key when redaction changes the ID; key in `app_meta`, shared by every process | Plain hash (dictionary-attackable for emails); documenting the merge as a limitation (breaks REQ-036.a) |
| A-17 | Reject `future_timestamp` (skew > 1 h, configurable) and `older_than_retention` in `partialSuccess` | Clamping timestamps (silently rewrites data, breaks the waterfall) |

## Out of scope

PRD requirements are unchanged.
