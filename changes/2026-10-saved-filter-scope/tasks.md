# Tasks — Scope `delete_filter` to its Project

### DSF-01 · Fold the delta and fix the store
- **What**: apply `delta-spec.md` to the API spec (v1.3.1); `store.settings.delete_filter`,
  `BackendMethods.delete_filter` and `StorageBackend.delete_filter` take `project_id`.
- **Done**: contract test on both backends: deleting through another Project raises `NotFound`
  and leaves the filter in place.
- [x] 2026-10-31 · PR `fix/saved-filter-project-scope`
