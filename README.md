# reflex-agentlens

Self-hosted observability for AI agents, built with [Reflex](https://reflex.dev). Ingest
OpenTelemetry GenAI traces from LangGraph, PydanticAI, OpenAI Agents or any OTLP source and get
live run timelines, graph replay, tool-call inspection, token and cost analytics — in pure Python,
on your infra. Embed it as a page in your own app or run it standalone.

> **Status: pre-alpha, not usable yet.** The v0.1 specification is approved and phases P0–P6
> are built: storage on SQLite and PostgreSQL, OTLP ingest (HTTP and gRPC), the query layer, the
> runtime with its CLI, and the UI (Dashboard, Runs, Trace with span drawer, agent graph and
> replay, Sessions, Settings), served standalone by `serve` with a Content-Security-Policy or
> embedded in a host Reflex app. Packaging (P7) is in progress: the wheel ships the pre-built
> frontend and boots without Node or Bun; the container image, `compose.yaml` and the production
> topology `compose.postgres.yaml` (PostgreSQL, scalable collectors, Caddy with TLS) work when
> built from source; the release workflow comes next. Nothing is published on PyPI or GHCR yet.
> See [Roadmap](#roadmap).

## Why

- **Privacy.** Prompts and completions often contain PII/PHI and business data. agentlens keeps
  them on your machine or your servers. No SaaS, no telemetry sent anywhere.
- **Small footprint.** One Python process with an embedded SQLite database, or one container plus
  PostgreSQL. No ClickHouse, Redis, Kafka or object storage.
- **Agent semantics, not just spans.** Runs, Sessions, LLM calls rendered as conversations, tool
  calls, approvals, agent graphs and cost — computed from the standard
  [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/).
- **Inside your app.** The pages can be mounted in a host Reflex app and reuse its login.

## What v0.1 will do

| Area | Planned capability |
|---|---|
| Ingestion | OTLP/HTTP (protobuf and JSON) and OTLP/gRPC for traces and metrics; API keys per project; durable group commit |
| Normalization | GenAI semconv (current and deprecated attributes) plus PydanticAI, OpenInference and LangSmith dialects; LLM calls, tool calls, approvals, agent graph nodes; redaction before storage |
| Cost | Versioned pricing table bundled with the package (no runtime fetch); exact decimal costs |
| Storage | SQLite (default) or PostgreSQL, same schema; retention by age and size; full-text search |
| UI | Dashboard, Runs, Trace (waterfall, conversation, graph replay), Sessions, Settings; live updates; charts with `reflex-silverpoint-react`, graphs with `reflex-mermaidjs` |
| Deployment | `uvx reflex-agentlens serve`, a single Docker container, or Docker Compose with PostgreSQL; an embeddable `mount(app, ...)` API |

Planned usage (not available yet):

```bash
uvx reflex-agentlens serve    # UI + OTLP/HTTP on :8000, OTLP/gRPC on :4317, SQLite under $XDG_DATA_HOME

# then point any OTel-instrumented agent at it
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8000
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer%20<api-key>"
```

From a source checkout you can already build the image and try it with Docker Compose:

```bash
docker build -t reflex-agentlens:dev .
AGENTLENS_IMAGE=reflex-agentlens:dev docker compose up -d --wait   # http://localhost:8000
docker compose exec agentlens cat /data/bootstrap-api-key          # API key of Project "default"
```

`compose.yaml` publishes on `127.0.0.1` only and runs without a UI login (`auth.mode=none`), so it
is meant for a laptop. Change the ports with `AGENTLENS_HTTP_PORT` / `AGENTLENS_GRPC_PORT`. When
you run the image yourself behind a different host port or a proxy, set
`AGENTLENS_SERVER_PUBLIC_URL` so the UI connects back to the right address.

For a shared server, `compose.postgres.yaml` runs PostgreSQL, the UI (`app`), horizontally
scalable collectors and Caddy, the only service that publishes ports (HTTPS with an automatic
certificate, OTLP/HTTP under `/v1/*`, OTLP/gRPC with TLS on 4317). Copy
`deploy/postgres.env.example` to `.env`, fill it in, then:

```bash
docker compose -f compose.postgres.yaml up -d --wait --scale collector=2
docker compose -f compose.postgres.yaml exec app cat /data/bootstrap-api-key
```

```python
import reflex as rx
import reflex_agentlens as agentlens

app = rx.App()
agentlens.mount(app, auth=my_auth, prefix="/agentlens")  # pages inside your own Reflex app
```

## How it is built

The project follows **spec-driven design**: specifications live in [`specs/`](specs/) and are
approved before code is written. Changes to approved specs go through Delta Specs in
[`changes/`](changes/).

| Document | Content |
|---|---|
| [Constitution](specs/constitution.md) | Project principles, stack constraints, licensing, process rules |
| [PRD](specs/prd/agentlens-v0.1.md) | Requirements with EARS acceptance criteria (`REQ-NNN.x`) |
| [API Spec](specs/api/agentlens-api-v1.md) | OTLP endpoints, embedding API, CLI, configuration |
| [Tech Design](specs/technical/agentlens-architecture.md) | Architecture, design decisions (DD-001…023), spike results |
| [Data Model](specs/data-model/agentlens-schema.md) | Schema for SQLite and PostgreSQL, critical queries |
| [Implementation Plan](specs/plans/agentlens-phase-1.md) | Phases P0–P10 |
| [Tasks](specs/tasks/agentlens-tasks.md) | Executable tasks with progress and execution log |

Architecture in one paragraph: a single process runs the Reflex backend (Granian, Starlette
routes for OTLP/HTTP and health) and a dedicated ingest thread with its own asyncio loop for
OTLP/gRPC and the durable writer. The frontend is pre-built at release time and shipped inside the
wheel, so no Node.js or Bun is needed at runtime. Storage uses SQLAlchemy 2.0 Core (async) over
SQLite or PostgreSQL, and every dashboard aggregate is computed in SQL.

## Repository layout

```
src/reflex_agentlens/   the package: core, ingest, store, query, components, pages, app, cli
scripts/                maintenance scripts (REQ traceability check)
tests/                  unit, golden, property, contract, integration, smoke, … suites
site/                   project website (uv workspace member, not published)
examples/               example instrumented agents (uv workspace member, not published)
specs/                  approved specifications (current truth)
changes/                Delta Specs under review
```

## Development

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). All tooling runs through `uv`.

```bash
uv sync --all-packages        # create .venv with the package, workspace members and dev tools
uv run pytest --cov           # tests + coverage gate (ingest + store + query >= 85 %)
uv run ruff check             # lint
uv run ruff format --check    # formatting
uv run mypy                   # strict type checking
uv run lint-imports           # layer and import contracts
uv run python scripts/check_req_coverage.py --report-only   # MUST criteria cited by tests
uv build                      # sdist + wheel (the wheel hook exports the frontend: needs Bun)
uv run reflex-agentlens --help   # CLI: serve, collect, demo, db, rebuild, purge, keys, version
```

To try the UI from a source checkout, export the frontend once (needs [Bun](https://bun.sh) or
Node; the running server never does) and start `serve`:

```bash
uv run python -m reflex_agentlens._webapp.build   # writes src/reflex_agentlens/_frontend/
uv run reflex-agentlens serve                     # UI and OTLP/HTTP on http://127.0.0.1:8000
```

Without the export, `serve` still runs the API and OTLP routes and `/` explains how to build the UI.

`uv build` runs a build hook (`hatch_build.py`) that exports the same frontend into the wheel,
together with `csp.json` (the hashes of its inline scripts), so an installed wheel needs no JS
runtime. `uv sync` (editable) skips it, and `AGENTLENS_SKIP_FRONTEND=1` builds a wheel without
the UI. Building a wheel from the sdist also needs Bun 1.4.4 or Node.

Tests marked `pg` run only when `AGENTLENS_TEST_PG_URL` points at a PostgreSQL ≥ 15 server; each
test works in its own throwaway schema:

```bash
docker run -d --name agentlens-pg -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:5432:5432 postgres:18
AGENTLENS_TEST_PG_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres uv run pytest
```

Branches are cut from `develop`; feature work goes in `feat/<task-id>-<slug>` and merges through a
pull request. Tests cite the requirement they verify (`REQ-NNN.x`). See [CONTRIBUTING.md](CONTRIBUTING.md) for the
full workflow and [AGENTS.md](AGENTS.md) for coding-agent guidance.

CI (GitHub Actions) runs lint, types, the import contracts and the traceability report; the tests
on Python 3.11–3.13 with SQLite and against PostgreSQL 15 and 18; a `smoke` job that exports the
frontend, boots `serve` and drives it (and a host app that mounts agentlens) in Chromium; a
`wheel` job that builds the wheel and the sdist and boots the wheel in a fresh venv with no Node or
Bun on `PATH` (UI and ingest within 30 s); a `docker` job that runs hadolint, builds the image,
scans it with trivy (no unwaived CRITICAL findings) and runs `tests/docker` (read-only root,
`collect` against PostgreSQL, `compose.yaml` up to a visible Run, `compose.postgres.yaml` with two
collectors behind Caddy and every span stored exactly once); and gitleaks plus pip-audit on
every pull request. Run the Docker tests locally with
`AGENTLENS_IMAGE=reflex-agentlens:dev uv run pytest tests/docker`. Run the smoke suite locally with
`AGENTLENS_SMOKE=1 uv run pytest tests/smoke` after building the frontend.

## Roadmap

| Phase | Content | State |
|---|---|---|
| P0 | Spikes: pre-built frontend in the wheel (S1), SQL performance on both backends (S2) | Done — results in Tech Design §11 |
| P1 | Package skeleton, quality gates, CI, `core` (errors, IDs, time, money, API keys, config, logging) | Done |
| P2 | Store: schema and migrations on SQLite and PostgreSQL, write path, reads, change feed, contract tests | Done |
| P3 | Ingest: OTLP/HTTP and gRPC, semconv mapping, derivation, redaction, pricing, admission and limits | Done |
| P4 | Query: DTOs, keyset cursors, Runs/Trace/Sessions/Dashboard queries, agent graph, admin service | Done |
| P5 | Runtime, CLI and ops: lifespan, bootstrap, standalone auth, graceful shutdown, self-telemetry | Done |
| P6 | UI: Runs, Trace, Sessions, Dashboard, Settings; standalone app and CSP; browser smoke suite; [silverpoint issue drafts](docs/upstream-issues.md) | Done |
| P7 | Delivery: wheel with the pre-built frontend, PyPI, container image, compose files | In progress — wheel, image and both compose topologies done |
| P8 | Recipes and benchmarks | Planned |
| P9 | Docs and site | Planned |
| P10 | Release | Planned |

## License

[MIT](LICENSE) © Ernesto Crespo
