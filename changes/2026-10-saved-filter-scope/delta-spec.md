# Delta — Scope `delete_filter` to its Project

Base: API v1.3. Status: adopted and folded in the same PR (see `proposal.md`).

## MODIFIED · API §4 protocol (`StorageBackend`)
- **Before**: `async def delete_filter(self, filter_id: str) -> None: ...`
- **After**: `async def delete_filter(self, project_id: str, filter_id: str) -> None: ...`

## MODIFIED · API §6 query-layer table, row `list_saved_filters` / `save_filter` / `delete_filter`
- **Appended**: "`delete_filter` only matches a filter of the given Project; one of another Project
  raises `NotFound`, like a missing one."
