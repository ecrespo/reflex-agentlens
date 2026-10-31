# Delta — Ingest prerequisites (Analyze A-04, A-07, A-08, A-17)

Base: PRD v1.1, API v1.2, Tech Design v1.3, Data Model v1.1, Tasks v1.1.
Evidence: Analyze report `specs/analyze/agentlens-analyze-2026-10-31.md` ("before P3 starts").
Status: adopted under the maintainer's standing authorization of 2026-10-31 ("continue with all
implementation phases without approval"), folded into `specs/` in the same PR (`docs/p3-prep`).
The maintainer can revert any decision below; each one is isolated.

## D1 — Per-batch isolation inside a commit group (A-04, HIGH)

### MODIFIED · Tech Design → DD-003 (Decision, appended)
- **After**: "Each batch of a group runs inside its own `SAVEPOINT`. If a statement of one batch
  fails with a **data error** (integrity or data exception: SQLSTATE classes `22` and `23` on
  PostgreSQL, constraint errors on SQLite), the writer rolls back to that savepoint, resolves the
  batch's future with `BatchRejected` and commits the rest of the group. Any other error (lost
  connection, I/O, lock timeout) fails the whole group with `StorageUnavailable`. A PostgreSQL
  deadlock retries the whole group once (DD-004). Change-feed events are published only for the
  batches that committed."

### MODIFIED · Tech Design → §3.3 Flow 1, steps 11–12
- **After**: "11. Writer groups queued batches (≤ 50 ms or ≤ 5,000 spans) into ONE transaction,
  one SAVEPOINT per batch, `write_batch()` semantics per batch … 12. COMMIT. Each committed
  batch's future gets its `WriteResult`; each rolled-back batch's future gets `BatchRejected`."

### ADDED · Tech Design → §3.3 error table
| Failure | Handling |
|---|---|
| One batch of a group hits a data error (e.g. its Project was deleted while the key was still in the auth cache) | Roll back to that batch's savepoint; its future resolves to `BatchRejected` → HTTP 400 / `INVALID_ARGUMENT` (not retryable); the rest of the group commits |

### ADDED · API §3.3 failure table
| Condition | HTTP | gRPC | Retryable | `Retry-After` / `RetryInfo` | REQ |
|---|---|---|---|---|---|
| Batch rejected by a database constraint | 400 | `INVALID_ARGUMENT` | No | — | 009.a |

`google.rpc.Status.message` is `"batch rejected"`.

### ADDED · API §5.1 (after `WriteResult`)

```python
@dataclass(frozen=True, slots=True)
class BatchRejected:
    reason: str                     # fixed key, e.g. "constraint_violation"; never data

async def write_group(self, batches: Sequence[NormalizedBatch]) -> list[WriteResult | BatchRejected]
```

"`write_group` applies §5.1 to each batch in one transaction with one savepoint per batch and
returns one result per batch, in order. `write_batch(b)` is `write_group([b])[0]`, raising
instead of returning `BatchRejected`."

## D2 — Redact before truncating (A-07, MEDIUM)

### MODIFIED · Tech Design → §3.3 Flow 1, step 9
- **Before**: "Per span: validate → truncate long values → redaction (rules, then hook) → content
  drop …"
- **After**: "Per span: validate → redaction (rules, then hook) → truncate long values → content
  drop …". The per-rule regex budget (25 ms per value, fail closed) bounds the cost of redacting
  values longer than the truncation limit.
- **Test**: canary with a secret that straddles the 256 KiB limit: no prefix of it is stored.

## D3 — Session natural key survives redaction (A-08, MEDIUM)

### MODIFIED · Data Model → §2.8 `sessions.external_id`
- **After**: "Conversation ID as sent. **If redaction changes it**, the stored value is
  `<redacted value> + '~' + hex(HMAC-SHA256(session_key, raw ID))[:16]`: unrelated conversations
  stay apart, and the raw ID is never stored. Longer values are truncated and hashed as before."

### MODIFIED · Data Model → §2.1 `app_meta` keys
- **After**: adds `session_key`: 32 random bytes, hex. Created on first use with insert-or-ignore,
  so every process that shares the database uses the same key. It is never logged or shown.

### ADDED · API §5 protocol method
`async def get_or_create_secret(self, name: Literal["session_key"]) -> bytes` (store-owned,
used by the ingest engine at start-up).

## D4 — Clock skew rule (A-17, MEDIUM)

### ADDED · API §3.6 span validation
| Rule | Rejection reason key |
|---|---|
| `start_time_unix_nano` > receive time + `ingest.max_clock_skew` | `future_timestamp` |
| `end_time_unix_nano` < receive time − the Project's retention max age (when > 0) | `older_than_retention` |

### ADDED · API §8 `[ingest]` configuration
| Key | Type | Default | Meaning | REQ |
|---|---|---|---|---|
| `[ingest] max_clock_skew` | duration | `1h` | Spans starting further in the future are rejected (`future_timestamp`) | 010 |

- **Reason**: a future span would keep its Run `in_progress` and escape age-based retention. A
  span older than retention would be purged on the next run. Both are reported through
  `partialSuccess`, which REQ-010 already covers.

## Unchanged

PRD requirements are unchanged: every decision refines how existing MUST criteria (REQ-009.a,
REQ-010, REQ-036.a, REQ-039.a) are met.
