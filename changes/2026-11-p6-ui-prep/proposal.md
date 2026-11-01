# Proposal — UI prerequisite (Analyze A-12)

> Status: `APPROVED` under the maintainer's standing authorization of 2026-10-31 ("continue with
> all implementation phases without approval"). Folded into `specs/` in the same PR. The
> maintainer can revert the decision; it is isolated in `delta-spec.md`.
> Base specs affected: PRD v1.1 → v1.2 (REQ-191.a wording), API v1.4.3 → v1.4.4 (§7.2 sentence),
> Tasks v1.4 → v1.5 (P6 detailed) · Date: 2026-11-01 · Author: Claude (for Ernesto Crespo)

## Problem

The Analyze report lists A-12 as "before P6": REQ-191.a says the host auth hook is called "on
each page load and event", while API §7.2 caches the hook's result per client for 30 s. A test
of REQ-191.a as written fails against the API as specified.

## Decision (details in `delta-spec.md`)

| Option | Verdict |
|---|---|
| **Page loads always call the hook; events may reuse the result for ≤ 30 s, per client token only** | **Adopted.** A logout or a role change is seen at the next navigation; an open page keeps its events fast even when the host hook reads a database |
| Drop the cache (call on every event) | Rejected: every keystroke-driven event (filter bar, live refresh every ≤ 10 s) would hit the host hook |
| Keep "cached 30 s" for page loads too | Rejected: a logged-out user could still load pages for 30 s |

## Out of scope

No other requirement changes. No schema or code change in this PR (the hook is implemented in
P6-03).
