# AGENTS.md — reflex-agentlens

Guidance for coding agents (and humans) working in this repository. Keep it short; the specs hold
the detail.

## Read first

- [`specs/constitution.md`](specs/constitution.md) — project principles. They win over anything else.
- [`specs/tasks/agentlens-tasks.md`](specs/tasks/agentlens-tasks.md) — what to do next, with the
  execution log. Each task names its REQ IDs, files, dependencies and Done check.
- The PRD, API Spec, Tech Design and Data Model under [`specs/`](specs/) — the current truth.

## Process (spec-anchored, Constitution Art. 10)

- Implement only what an approved spec describes. If the code shows a spec is wrong: stop, update
  the spec or open a Delta Spec under `changes/<yyyy-mm>-<slug>/`, then continue.
- Work in batches of 3–5 tasks, then stop for review. Tick tasks and add an execution-log row in
  the tasks file.
- Branch from `develop` as `feat/<task-id>-<slug>` (spikes: `spike/…`, never merged); merge
  through a pull request. Conventional Commits. Everything in English.

## Code rules

- Layers and import contracts (Tech Design §5.1, enforced by `lint-imports`): `core` is a leaf;
  `ingest`, `store`, `query` and `core` never import Reflex; `pages`/`components` never import `store`.
- `mypy --strict` on `core`, `ingest`, `store`, `query`. No `print()` outside `cli` and `scripts`.
- Money is `Decimal`, timestamps are integer nanoseconds. Aggregates are SQL, never Python loops.
- In `ingest`, `store` and `query`, the failing test comes first or in the same commit.
- Every test cites the criterion it verifies (`REQ-NNN.x`) in its name or docstring.

## Commands (always through uv)

```bash
uv sync --all-packages
uv run ruff check && uv run ruff format --check
uv run mypy
uv run lint-imports
uv run pytest --cov                          # coverage gate: ingest+store+query >= 85 %
AGENTLENS_TEST_PG_URL=postgresql+asyncpg://… uv run pytest -m pg
uv run python scripts/check_req_coverage.py --report-only
```
