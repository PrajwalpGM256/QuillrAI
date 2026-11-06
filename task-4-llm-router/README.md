# llm-router

An OpenAI-compatible LLM gateway with a per-tenant, token-aware rate limiter in SQLite and primary-to-secondary failover.

---

## What it does

- `POST /v1/chat/completions` admits each request against a rolling 60 s budget of 50,000 tokens per tenant API key, kept in an on-disk SQLite file that several router processes can share.
- Forwards admitted requests to the primary provider and fails over to the secondary only when the primary returns 429 or has not answered within 3000 ms.
- Every failure returns one sanitized error shape.

The providers here are `mock_provider/`, configurable at runtime. No API keys are needed.

---

## Quickstart

```sh
uv sync                                  # Python 3.13, pinned in uv.lock
uv run python scripts/run_stack.py       # primary :9201, secondary :9202, router :8200
```

From Git Bash in a second terminal (PowerShell 5.1 strips the inner quotes from the JSON):

```sh
R=http://127.0.0.1:8200/v1/chat/completions
J="Content-Type: application/json"
B='{"messages":[{"role":"user","content":"hello"}]}'

# happy path: "id":"chatcmpl-primary"
curl -s $R -H "Authorization: Bearer demo-tenant-a" -H "$J" -d "$B"

# failover: the primary returns 429, so the answer is "id":"chatcmpl-secondary"
curl -s localhost:9201/_mock -H "$J" -d '{"status":429}'
curl -s $R -H "Authorization: Bearer demo-tenant-a" -H "$J" -d "$B"
curl -s -X POST localhost:9201/_mock/reset

# rate limit: each call is charged 30,000 tokens, so the third gets 429 with Retry-After
curl -s localhost:9201/_mock -H "$J" -d '{"usage_total":30000}'
for i in 1 2 3; do curl -s -o /dev/null -D - $R -H "Authorization: Bearer demo-tenant-b" -H "$J" -d "$B" | grep -iE "^HTTP|retry-after"; done
curl -s -X POST localhost:9201/_mock/reset
```

`POST /_mock` accepts `status`, `delay_ms`, `usage_total`, and `malformed`; `GET /_mock/stats` reports requests, completions, and cancellations. Window state persists in `.state/router.db`; delete it to start fresh.

---

## Fallback policy

| Primary outcome | Action | Secondary outcome | Final response |
|---|---|---|---|
| 200, valid completion | Return it | Not called | 200, usage settled |
| 429, or no response in 3000 ms | Fail over | 200, valid completion | 200, usage settled |
| 429, or no response in 3000 ms | Fail over | 429 | 503 `upstream_rate_limited` |
| 429, or no response in 3000 ms | Fail over | No response in 3000 ms | 503 `upstream_timeout` |
| 429, or no response in 3000 ms | Fail over | Other status, connection error, malformed body | 503 `upstream_unavailable` |
| Other 4xx | No retry | Not called | 400 `upstream_rejected_request` |
| 5xx, or 200 with a malformed body | No retry | Not called | 502 `upstream_error` |
| Connection error | No retry | Not called | 502 `upstream_error` |

On timeout the primary `asyncio.Task` is cancelled and awaited until done before the secondary starts, so the abandoned call never keeps running in the background. Other failures do not fail over because the request or the provider is broken, and a billed, non-idempotent request should not be sent twice.

---

## Rate limiter

A row counts against a tenant while `ts > now - 60`. Admission is one `BEGIN IMMEDIATE` transaction: delete expired rows, sum the tenant's window, then insert a `reserved` row for the input estimate or reject with 429 and a `Retry-After` computed from the oldest rows that must expire. Eviction runs inside that transaction, not on a timer, so the sum is exact at the instant of every decision and no other process can write between the sum and the insert. On success the row is settled to the provider's `usage.total_tokens`; on any failure, including client cancellation, it is deleted. **The gap:** output tokens are unknown until the response arrives, so admission sees only input estimates, and after settlement a tenant's window can exceed 50,000 by the unpredicted output tokens. Admission itself never exceeds the limit.

---

## Token estimation

`ceil(utf8_bytes(content) / 3) + 4` per message. It errs high on purpose (about 4 characters per English token, so roughly a quarter over) so admission under-reserves less often, and settlement refunds the difference.

---

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| SQLite thread | Every call runs on one dedicated worker thread that owns the connection, never the event loop | `sqlite3` blocks; on the loop, every request would queue behind each DB call, including waits for another process's write lock |
| Journal | `journal_mode=WAL`, `busy_timeout=5000` | Readers proceed during a write, and several processes can share the file |
| Lock | `BEGIN IMMEDIATE` | Takes the write lock before the sum, so sum and insert cannot interleave with another writer |
| Durability | `synchronous=FULL` | A committed reservation survives power loss |
| Row ids | `AUTOINCREMENT`, and settle/release match on id and tenant | Without it SQLite reused an evicted reservation's id, and a late settle overwrote another tenant's newer row (caught by `test_limiter.py`) |
| Clock | Wall clock behind an injectable `Clock` | Rows must compare across processes and restarts; tests inject a fake clock |
| Streaming | `stream: true` gets 400 | Output tokens arrive only at stream end, and failover is impossible once bytes reach the client |
| Oversized request | Estimate over 50,000 gets 400 `token_budget_exceeded`, no row | It can never be admitted, so 429 would make clients retry forever |
| Settle and release | Awaited under `asyncio.shield` | Cancelling the request must not drop the charge or leak the reservation |
| Tenant | `Authorization: Bearer <key>` checked against `TENANT_KEYS` in constant time; SQLite stores SHA-256(key), logs an 8-hex fingerprint | Accepting any key would let clients mint keys to bypass the limit |
| Upstream | Each route sends its own `*_MODEL` and `*_API_KEY`; the tenant key is never forwarded | The secondary is usually a different model, and the tenant key is not a provider credential |

---

## Error schema

```json
{"error": {"type": "upstream_error", "code": "upstream_timeout", "message": "All model providers timed out. Try again later.", "request_id": "e42a9214c47845438b41510a386233e2"}}
```

Messages are fixed strings per code, and `request_id` is also sent as `X-Request-ID`. Upstream URLs, model names, stack traces, raw upstream bodies, credentials, and tenant keys never reach the client; details stay in server logs, keyed by `request_id`.

| Status | Codes |
|---|---|
| 400 | `malformed_json`, `invalid_messages`, `streaming_not_supported`, `token_budget_exceeded`, `upstream_rejected_request` |
| 401 | `missing_api_key`, `invalid_api_key` |
| 404 / 405 / 413 | `not_found` / `method_not_allowed` / `request_too_large` |
| 429 | `rate_limited` (with `Retry-After`) |
| 500 | `internal_error` |
| 502 | `upstream_error` |
| 503 | `upstream_rate_limited`, `upstream_timeout`, `upstream_unavailable`, `limiter_unavailable` |

---

## Residual risks

- After settlement a window can exceed 50,000 by unpredicted output tokens (the two-phase gap).
- A primary connection error returns 502 and does not fail over; only 429 and the timeout do.
- A backward wall-clock step briefly lengthens the window (over-counts).
- The estimate is a byte heuristic, not a tokenizer; unusual inputs can be under-reserved until settlement.
- `asyncio.shield` around settle and release is untested: no test cancels a request at the exact moment its settle or release is running.

---

## Verification

```sh
uv run ruff check . && uv run pytest -q     # single gate: lint + 85 tests
```

| Test file | Proves |
|---|---|
| `test_limiter.py` | Window boundary with a fake clock: full at `T0`, 429 at `T0+59.999`, admitted at `T0+60.0` with no eviction call and the expired rows gone. WAL, `synchronous=FULL`, one `BEGIN IMMEDIATE` per write (SQL trace), rollback, restart persistence, Retry-After, settle/release, and the stale-settle id-reuse case |
| `test_limiter_model.py` | The independent oracle: a Hypothesis state machine runs random reserve, settle, release, and clock advances against a pure-Python model of the window, decision by decision, catching bugs that tests written against the same SQL cannot see; admissions never exceed the limit |
| `test_event_loop.py` | SQL never runs on the event loop thread: every statement executes on the limiter's worker thread (SQL trace plus thread ids). During a 500 ms DB stall a ticker stays within 50 ms and a request that needs no DB is answered immediately |
| `test_concurrency.py` | 120 simultaneous 1,000-token requests admit exactly 50; the window sum sampled during a mixed burst never exceeds 50,000; tenants are independent |
| `test_cancellation.py` | When the secondary receives the request, the primary task is already done and cancelled and the primary provider has recorded the cancellation. Client cancellation cancels the primary and releases the reservation; no primary tasks are left running |
| `test_fallback.py` | Each row of the policy table, with the reservation released on every failure and settled on success; per-route model and credential |
| `test_errors.py` | 22 failure modes return exactly `{"error": {type, code, message, request_id}}` matching `X-Request-ID`, with no upstream URL, model name, credential, tenant key, traceback, SQLite detail, or upstream body canary in body or headers; tenant keys never appear in logs |
| `test_multiprocess.py` | Two router processes sharing one SQLite file admit exactly 50 of 80 concurrent 1,000-token requests between them; the file is in WAL mode |
| `test_live_timeout.py` | Over real sockets, a 5 s primary with a 300 ms timeout: the secondary answers in under 2 s and the primary provider observes the disconnect |
| `test_tokens.py`, `test_config.py` | Estimator formula and conservativeness (ASCII, CJK); invalid configuration exits 2 |

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `TENANT_KEYS` | required | Comma-separated tenant API keys |
| `PRIMARY_URL` / `SECONDARY_URL` | required | Provider chat completions endpoints |
| `PRIMARY_MODEL` / `SECONDARY_MODEL` | required | Model sent to each provider |
| `PRIMARY_API_KEY` / `SECONDARY_API_KEY` | unset | Provider credentials (must differ from tenant keys) |
| `PRIMARY_TIMEOUT_MS` / `SECONDARY_TIMEOUT_MS` | `3000` | Per-attempt deadline |
| `ROUTER_DB_PATH` | `.state/router.db` | SQLite file, shareable between processes |
| `TOKEN_LIMIT` / `WINDOW_SECONDS` | `50000` / `60` | Budget per key per rolling window |
| `MAX_REQUEST_BYTES` | `1048576` | Request body limit |
| `DB_BUSY_TIMEOUT_MS` | `5000` | Wait for another process's write lock |
| `HOST` / `PORT` | `127.0.0.1` / `8200` | Bind address |
| `LOG_LEVEL` | `INFO` | stderr log level |

---

## Project layout

```
router.py                entry point: load config (exit 2 if invalid), serve on 127.0.0.1
llm_router/
  limiter.py             SqliteWindowLimiter: worker thread, WAL, reserve / settle / release
  upstream.py            Router: timeout race, cancel-and-await, fallback policy
  app.py                 request pipeline, tenant auth, error handling
  errors.py              error schema and code catalog
  tokens.py              admission estimate
  clock.py               wall clock and fake clock
  config.py              environment config, validated at startup
  logging_setup.py       single stderr log handler
mock_provider/server.py  OpenAI-compatible mock with runtime behaviour and cancellation stats
scripts/run_stack.py     run both mocks and the router locally
tests/                   pytest suite; helpers.py wires the in-process stack, procs.py the subprocess one
```
