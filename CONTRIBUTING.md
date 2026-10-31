# Contributing to reflex-agentlens

Thanks for your interest. The project is pre-alpha and spec-driven: the specs in
[`specs/`](specs/) are approved before code is written, and every change traces back to them.

## Setup

Requirements: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and Docker for the PostgreSQL tests.

```bash
git clone https://github.com/ecrespo/reflex-agentlens.git
cd reflex-agentlens
uv sync --all-packages
uv run pytest
```

## Workflow

1. **Find or propose the spec.** Work on a task from
   [`specs/tasks/agentlens-tasks.md`](specs/tasks/agentlens-tasks.md). A change to behavior that is
   already specified needs a Delta Spec first (below). Bug fixes that restore specified behavior
   need only an issue.
2. **Branch from `develop`:** `feat/<task-id>-<slug>`, `fix/<slug>` or `docs/<slug>`.
3. **Write the test first** for `ingest`, `store` and `query` (or in the same commit). Cite the
   acceptance criterion the test verifies — `REQ-NNN.x` — in the test name or docstring.
4. **Run the checks** (CI runs the same):

   ```bash
   uv run ruff check && uv run ruff format --check
   uv run mypy
   uv run lint-imports
   uv run pytest --cov
   uv run python scripts/check_req_coverage.py --report-only
   ```

5. **Open a pull request into `develop`.** Releases are merged into `main` and tagged `vX.Y.Z`.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `build:`,
`ci:`, `test:`, `refactor:`, `chore:`, with an optional scope (`feat(ingest): …`). The changelog
is derived from them. Code, comments, docs and commit messages are in English.

## Delta Specs

To change specified behavior, create `changes/<yyyy-mm>-<slug>/` with:

- `proposal.md` — problem with evidence, scope, impact, Constitution check;
- `delta-spec.md` — ADDED / MODIFIED / REMOVED entries against the files in `specs/`;
- `tasks.md` — the tasks, each citing the new or modified REQ IDs.

After approval and implementation, the delta is folded into `specs/` in the same pull request that
closes it, and the folder moves to `changes/_archive/`. REQ IDs are never reused.

## PostgreSQL tests

Tests marked `pg` are skipped unless `AGENTLENS_TEST_PG_URL` is set:

```bash
docker run -d --name agentlens-test-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=agentlens_test \
  -p 55432:5432 postgres:18
AGENTLENS_TEST_PG_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/agentlens_test \
  uv run pytest -m pg
```

CI runs them against PostgreSQL 15 and 18.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
