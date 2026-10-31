# Proposal — Scope `delete_filter` to its Project

> Status: `APPROVED` under the maintainer's standing authorization of 2026-10-31 ("continue with
> all implementation phases without approval"). Folded into `specs/` in the same PR. The
> maintainer can revert it. Base spec affected: API v1.3 → v1.3.1 · Date: 2026-10-31 ·
> Author: Claude (for Ernesto Crespo)

## Problem

API §6 says saved filters are "per Project", and any viewer with access to that Project may manage
them (REQ-102.a). The protocol method `delete_filter(filter_id)`, however, has no Project in its
signature. A viewer who can see only Project A could therefore delete a filter of Project B if they
learned its ID (IDOR). The issue was flagged by an automated security review of `store/settings.py`.

## Decision

`delete_filter(project_id, filter_id)`: the delete only matches a filter of that Project. A filter
of another Project raises `NotFound`, exactly like a missing one, so callers can't tell the two
apart. The caller (the query layer, P4) passes the Project it has already authorized for the viewer.

`save_filter` already scopes its updates (an `id` from another Project raises `Conflict`), and
`list_saved_filters` already takes a `project_id`, so neither changes.
