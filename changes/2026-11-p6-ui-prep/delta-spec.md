# Delta — UI prerequisite (Analyze A-12)

Base: PRD v1.1, API v1.4.3, Tasks v1.4.
Evidence: Analyze report `specs/analyze/agentlens-analyze-2026-10-31.md` ("Before P6: A-12").
Status: adopted under the maintainer's standing authorization of 2026-10-31, folded into `specs/`
in the same PR (`spec/P6-ui-prep`).

## D1 — Auth-hook calls and caching (A-12, MEDIUM)

### MODIFIED · PRD → REQ-191.a
- **Before**: WHERE agentlens is embedded, THE SYSTEM SHALL call a host-provided auth hook on each
  page load and event, and expect a viewer identity (ID, display name, `is_admin`) or
  "unauthenticated".
- **After**: WHERE agentlens is embedded, THE SYSTEM SHALL call a host-provided auth hook on each
  page load and before each event that reads or changes data, and expect a viewer identity (ID,
  display name, `is_admin`) or "unauthenticated". For events (not page loads) the result MAY be
  reused for at most 30 s, and only for the same client token.

### MODIFIED · API → §7.2 (Namespacing paragraph)
- **Before**: All event handlers call the auth hook (cached per client token for 30 s) before
  touching data.
- **After**: Every page `on_load` calls the auth hook (never cached). Every other event handler
  calls it before touching data; its result may be reused for at most 30 s for the same client
  token (`[REQ-191.a]`), and is never shared across client tokens.

### Tests (P6-03)
- A page load after the hook starts returning "unauthenticated" renders no data, even within
  30 s of the last successful call.
- An event within 30 s reuses the cached viewer (one hook call); after 30 s (injected clock) the
  hook is called again; another client token never sees the cached viewer.
