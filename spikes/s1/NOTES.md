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
