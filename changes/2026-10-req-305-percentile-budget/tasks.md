# Tasks — REQ-305 percentile budget

All tasks trace to REQ-305.a, REQ-305.b or REQ-141.f (PRD v1.2 after fold).

### D305-01 · Fold the delta into `specs/`
- **What**: Apply `delta-spec.md` to the PRD, Tech Design, Data Model and Tasks; bump versions;
  move this folder to `changes/_archive/`.
- **Depends on**: maintainer approval of this change.
- **Done**: `specs/` contains no reference to the old REQ-305.a wording; PR merged.
- [ ]

### D305-02 · Separate latency query in `query` (planned in P5)
- **What**: `QueryService.dashboard_latency(filters)` returns Q10c; `dashboard(filters)` no longer
  includes it. Bounded-rows test (REQ-106.h) for both.
- **REQ**: REQ-305.a, REQ-305.b, REQ-106.c
- **Done**: contract test: identical values on SQLite and PostgreSQL.
- [ ]

### D305-03 · Dashboard loads the latency panel independently (planned in P6)
- **What**: the dashboard state fetches `dashboard_latency` in its own background event; the chart
  shows a loading state.
- **REQ**: REQ-141.f
- **Done**: UI test with a slowed latency query: other panels render first.
- [ ]

### D305-04 · Benchmarks split (planned in P8-06)
- **What**: the REQ-305 benchmark reports .a and .b separately on the reference machine.
- **REQ**: REQ-305.a, REQ-305.b
- **Done**: both pass, or ladder step 1 reopens for Q10c via a new Delta Spec.
- [ ]
