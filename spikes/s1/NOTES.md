# Spike S1 notes (input for the S1 report, Tasks P0-06.1)

Machine: maintainer laptop, Linux, Python 3.13.9, uv 0.11.7, Bun (in ~/.bun), Node 24 also on PATH.
Versions resolved: reflex 0.10.1 (+ reflex_base 0.10.1), granian 2.8.7, starlette 1.10.0,
reflex-silverpoint-react 0.8.1, reflex-mermaidjs 0.2.1.

## P0-02.1 — export (2026-10-31)

- `reflex export --frontend-only --no-zip` works with `app_module_import="reflex_agentlens._webapp.app"`
  and an `rxconfig.py` outside the package (`spikes/s1/`). ~28 s wall time. Output in
  `.web/build/client/` (index.html, assets/, external/, 404.html, __spa-fallback.html, sitemap.xml).
- **Reflex 0.10 does not depend on pydantic.** It must be a direct runtime dependency (already in
  Tech Design §9.3).
- **`telemetry_enabled` defaults to `True` in Reflex.** Every rxconfig we write (build and runtime)
  must set `telemetry_enabled=False` (Constitution Art. 3, REQ-324). Add a test.
- Warnings to resolve in the real `_webapp` rxconfig: `SitemapPlugin` enabled implicitly (disable it:
  no sitemap needed), implicit Radix Themes enablement deprecated (configure
  `rx.plugins.RadixThemesPlugin()` explicitly or avoid Radix components).
- **Bundle size: 28 MB uncompressed** (gz siblings are also shipped). Largest: Mermaid icon chunks
  (`icons-*.js` 7.4 MB + 2.9 MB), ZenUML (3.1 MB), ELK layout (1.4 MB), global CSS 684 KB.
  Relevant to REQ-203.b (image ≤ 500 MB, fine) and wheel size; consider whether reflex-mermaidjs
  can exclude zenuml/elk/icons from the bundle (upstream option, same author).
- External hosts appearing as strings in the bundle: mostly doc/licence URLs (chevrotain.io, sketch.com,
  reactrouter.com, …). Potential runtime fetches: `cdn.jsdelivr.net` and fontawesome (Mermaid icon
  packs / Font Awesome), both disabled by `font_awesome=False, icon_packs=[]`. Verified at runtime
  in P0-05.2.
- After the first Reflex runs, an **empty `assets/external/` directory appeared in the repo root**
  (cwd at the time). A plain `import reflex, reflex_silverpoint_react, reflex_mermaidjs` in a clean
  directory writes nothing, so it comes from another Reflex code path (likely asset resolution when a
  component with a local JS module is created). To pin down in P0-05.3 with an audit hook.
- `reflex export` also writes `reflex.lock/` (bun.lock + package.json: frontend dependency lock,
  committed for reproducibility), `requirements.txt` and `.gitignore` into the rxconfig directory.

## P0-02.2 — build hook (2026-10-31)

- `hatch_build.py` (custom hook) runs the export and force-includes `.web/build/client` as
  `reflex_agentlens/_frontend/`. `uv build --wheel` → 14 MB wheel, 309 files, ~7 s with a warm Bun cache.
- **The isolated build env does not have the package installed**: the hook must put `src/` on
  `PYTHONPATH` for the `reflex export` subprocess, and the hook needs Reflex + UI deps declared as
  hook `dependencies` in `[tool.hatch.build.targets.wheel.hooks.custom]`.
- Editable installs (`uv sync`) skip the export (`version == "editable"`), so syncing needs no JS runtime.
  `AGENTLENS_SKIP_FRONTEND=1` builds a wheel without the frontend (for fast CI jobs).
- **Open for P7-01:** `uv build` without `--wheel` builds wheel-from-sdist. The sdist must then contain
  the `_webapp` module, the build rxconfig and `reflex.lock/`, and building from sdist needs Bun/Node on
  the build machine. Decide: ship sdists that require a JS runtime to build, or build wheels only from
  the repo (sdist marked as "frontend not included").

## P0-03.1 — boot from the wheel with no JS runtime (2026-10-31) — PASSED

- Clean venv, wheel installed, booted with `env -i PATH=/usr/bin:/bin` (no node, no bun on PATH) via
  `boot.py`: copies `_frontend` to `{data_dir}/web/<version>/.web/build/client`, writes a runtime
  rxconfig (telemetry off), sets `REFLEX_WEB_WORKDIR`, `REFLEX_STATES_WORKDIR`, `REFLEX_ENV_MODE=prod`,
  `__REFLEX_MOUNT_FRONTEND_COMPILED_APP=true`, `__REFLEX_SKIP_COMPILE=true` (internal env vars carry a
  `__` prefix), `GRANIAN_WORKERS=1`, then calls
  `reflex.utils.exec.run_backend_prod(host, port, LogLevel.INFO, mount_frontend_compiled_app=True)`.
- Workdir preparation: 0.03–0.08 s. `GET /` 200, `/ping` → pong.
- Browser smoke (`smoke.py`, Playwright Chromium): first render 0.84 s; chart + Mermaid SVG rendered;
  DTO rows 2 → 22 after 20 clicks; event round-trip p50 66.7 ms, max 68.5 ms; **no requests to
  foreign origins, no console errors**.

## P0-03.2 — baked backend URL / custom port (2026-10-31) — PASSED (A6 not needed)

- The export bakes `localhost:8000` into `assets/reflex-env-*.js` (6 URLs: event, ping, upload, …).
  The client's `getBackendURL` swaps the hostname for `window.location.hostname` (and drops the port
  on https), but keeps the baked **port** on http — so a non-8000 port breaks the websocket.
- Fix in `boot.py`: rewrite `localhost:8000` → `localhost:<port>` in the copied env module and
  regenerate/remove its `.gz/.br/.zst` siblings (Starlette's precompressed static handler would serve
  the stale compressed file otherwise).
- Port 9123: rewrite of 6 URLs, start → first `200` in **6.3 s** (dominated by importing Reflex +
  the app module), smoke identical to 8000 (first render 0.77 s, p50 66.6 ms, rows 2 → 22, no foreign
  requests, no console errors).
- Behind an https reverse proxy the port is dropped client-side, so the proxy origin is used: fine.
- **Finding (REQ-200.c):** binding an occupied port makes Granian print a raw Rust traceback
  ("Address already in use (os error 98)") and the process does not exit cleanly. The CLI must
  pre-check the port with a socket bind and exit with code 3 and a one-line message (P5-03).
- **Finding (startup budget):** 6.3 s start → first 200 on a dev laptop; record against the
  startup NFR in P8 bench, and consider lazy imports in the `serve` path.

## P0-04.1 — ingest thread with grpc.aio next to Granian (2026-10-31) — PASSED

- **Granian's `Server` runs the app in a spawned worker process even with `GRANIAN_WORKERS=1`**
  (main PID ≠ worker PID). Anything started in the boot script before `run_backend_prod` lives in
  the wrong process. The ingest thread therefore starts from a **Reflex lifespan task** (an
  `asynccontextmanager` registered with `app.register_lifespan_task`, supported by Reflex 0.10), which
  runs inside the worker. Code: `src/reflex_agentlens/_webapp/ingest_spike.py`.
- OTLP/HTTP `POST /v1/traces` is a Starlette route passed as `rx.App(api_transformer=…)` (Reflex mounts
  its own app under it); the handler forwards raw bytes to the ingest loop with
  `asyncio.run_coroutine_threadsafe` + `asyncio.wrap_future`. gRPC `TraceService/Export` is a
  `grpc.aio` server on the ingest thread's own loop (grpcio 1.85.1, opentelemetry-proto).
- `opentelemetry-sdk` client with both exporters (`otlp_client.py`): **10,000 / 10,000 spans counted
  on HTTP and on gRPC** (20 requests each, batches of 512).
- UI responsiveness (`ui_latency.py`, 200 clicks): idle p50 66.6 / p95 67.9 ms; **during a 25 s export
  at ~34k spans/s** (430k spans per protocol) p50 66.7 / p95 67.3 ms. No measurable GIL impact
  *with parse-and-count work only*; the real normalizer + DB writes are heavier and must be
  re-measured in P3/P8 (DD-001 concern stays open but unblocked).

## P0-04.2 — shutdown ordering (2026-10-31) — PASSED

`shutdown_test.py`: SIGTERM to the Granian main process 4 s into an export burst (65k HTTP + 60k
gRPC spans received so far). Log order: `Shutting down granian` → `Stopping worker-1` → lifespan exit
→ ingest loop drained → gRPC server stopped → thread joined (`alive=False`) → `Granian shutdown
completed`. **Exit code 0 after 0.03 s**, `:9123` and `:4317` released, no leftover process. The
lifespan exit is the right place for the REQ-331 drain (HTTP receiver is already closed by then).

## P0-05.1 — CSP with hashes (2026-10-31) — PASSED

- `boot.py --csp` hashes every inline `<script>` of the copied `*.html` (11 distinct hashes: 9 in
  `index.html`) and serves `default-src 'self'; script-src 'self' <hashes>; style-src 'self'
  'unsafe-inline'; connect-src 'self' ws://host:port; img-src 'self' data:; font-src 'self';
  object-src 'none'; base-uri 'self'; frame-ancestors 'none'` on every backend response (ASGI
  middleware as a second `api_transformer`, `csp_spike.py`).
- Playwright with the header enforced: page fully interactive (rows 2 → 22, Mermaid SVG, chart
  hover), **0 `securitypolicyviolation` events, 0 console errors, no `unsafe-eval` needed** (Mermaid
  `securityLevel=strict` works without eval).
- Test-harness gotcha: Playwright `page.wait_for_function("<string>")` uses `eval` and is blocked by
  the CSP. Use locator assertions (`expect(...).to_have_text`).
- **Hash stability:** the served `index.html` is byte-identical across requests, but **two exports of the
  same source produce different inline scripts** (the inline module imports content-hashed chunk
  names such as `manifest-<hash>.js`, and the bundle is not reproducible). So hashes must be computed
  from the HTML that is actually shipped: in the hatch hook (written next to the bundle) or at
  startup (as here). A hard-coded hash list in the source tree is not viable. `connect-src` must be
  derived from `server.public_url` (`wss://` behind TLS proxies).

## P0-05.2 — zero third-party requests (2026-10-31) — PASSED

Full session (load, chart hover sweep, Mermaid render, 20 state updates) with request interception:
**0 requests to non-app origins, 0 console errors**, also under the CSP above
(`font_awesome=False, icon_packs=[]`).

## P0-05.3 — import-time side effects (2026-10-31) — findings

`import_audit.py` (audit hook on sockets, writes, mkdir/rename/remove/symlink, subprocess), run in an
empty cwd. Results: `import-audit.json` (observe) and `import-audit-block.json` (block mode).
- **No network and no subprocess** at import of `reflex`, `reflex_silverpoint_react`,
  `reflex_mermaidjs` and the page module.
- **`import reflex_mermaidjs` writes into the current directory**: `reflex/assets.py` (`rx.asset(...,
  shared=True)`) creates `assets/external/reflex_mermaidjs/mermaidjs/` and symlinks
  `mermaid_diagram.jsx` from site-packages (mkdir, symlink, rename, remove). This is the stray
  `assets/external/` seen in P0-02.1.
- **Importing the app module creates `.states/`** in the cwd (`reflex/utils/path_ops.py` mkdir), unless
  `REFLEX_STATES_WORKDIR` is set first.
- Also: `__pycache__` writes for our own package (normal Python; disabled by `PYTHONDONTWRITEBYTECODE`
  or a read-only install).
- With every operation blocked (raise `PermissionError`), **all imports still succeed** (Reflex
  swallows the asset-link failure).
- Proposal for REQ-190.c (decide in P0-06.1): the test forbids network and subprocess at import, and
  allows filesystem writes only below the process cwd; the CLI `chdir`s to
  `{data_dir}/web/<version>` and sets `REFLEX_STATES_WORKDIR` **before** importing Reflex (`boot.py`
  already does), so nothing is written outside the data dir.
