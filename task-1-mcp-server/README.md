# refund-mcp-server

A stdio MCP server, built on the official Python SDK, that provides customer lookup and idempotent refunds.

---

## What it does

- `get_customer_record(customer_id)` returns a customer's profile, refundable balance, and past refunds. It is read-only.
- `trigger_refund(customer_id, amount, reason, idempotency_key)` issues an irreversible refund and returns the refund record.

Both tools declare an `outputSchema` and return `structuredContent`.

---

## Quickstart

```sh
uv sync                                                      # Python 3.13, mcp==2.3.0 pinned in uv.lock
uv run server.py                                             # serve MCP over stdio
npx @modelcontextprotocol/inspector@2.11.0 uv run server.py  # Inspector UI: Connect > Tools > List Tools
```

The seed customers are `CUST-00001` (250.00 USD refundable), `CUST-00002` (0.00 USD), and `CUST-00003` (1000.00 EUR).

---

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `get_customer_record` | `customer_id`: string, `^CUST-\d{5}$` | `customer_id`, `name`, `email`, `status`, `currency`, `refundable_balance`, `created_at`, `refunds[]` |
| `trigger_refund` | `customer_id`: string, `^CUST-\d{5}$`<br>`amount`: JSON number, `> 0`, at most 2 decimals<br>`reason`: string, 10 to 500 chars after trimming<br>`idempotency_key`: string, `^[A-Za-z0-9_-]{8,128}$` | `refund_id`, `customer_id`, `amount`, `currency`, `status`, `idempotency_key`, `replayed`, `created_at`, `remaining_refundable_balance` |

All arguments are required. The server rejects extra arguments (`additionalProperties: false`).

---

## Error model

The server returns a JSON-RPC error only when the `tools/call` envelope is broken. Every other failure is a tool result with `isError: true` and a stable `code`. Input validation uses `isError` instead of `-32602` because [SEP-1303](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1303) requires it (spec revision 2025-11-25), so that the calling model can read the error and correct its call.

| Failure | Channel | Code |
|---|---|---|
| Unknown tool, non-object `arguments` | JSON-RPC error | `-32602` |
| Argument fails schema | `isError` | `invalid_arguments` + `details[].field` |
| Customer does not exist | `isError` | `customer_not_found` |
| Refundable balance is 0 | `isError` | `nothing_refundable` |
| Amount exceeds balance | `isError` | `amount_exceeds_refundable` + `remaining_refundable_balance` |
| Same key, different arguments | `isError` | `idempotency_key_conflict` + `original_refund_id` |

Error text is `{"error": {"code": ..., "message": ...}}`. Branch on `code`, not `message`. SDK 2.3.0 reports an unknown tool as `isError`; `RefundMCPServer.call_tool` corrects it to `-32602`.

---

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Customer ID | `^CUST-\d{5}$`, case-sensitive | Widening the format later breaks no caller; narrowing it would |
| Money | JSON number on the wire, `Decimal` inside, strings like `"100"` rejected | Binary floats drift in currency math |
| State | Customers in a read-only JSON fixture, refunds and keys in SQLite | Idempotency must survive a restart, which is when retries happen |
| Idempotency key | Required, bound to a hash of the request | A refund sent without a key cannot be safely retried |
| Annotations | `destructiveHint: true`, `idempotentHint: false` | The refund moves money irreversibly, so clients should keep a human confirming it |
| Refund cap | Each customer has a `refundable_total` in the fixture; `refundable_balance` is that total minus refunds already issued, checked in the same transaction as the write | A model-driven destructive tool needs a hard ceiling, so a confused or injected agent cannot over-refund or refund one purchase many times under different keys. This is a customer-level pool standing in for per-charge limits, since the mock has no orders table |

---

## Idempotency

The required `idempotency_key` makes `trigger_refund` safe under agent retry loops, where the same refund can arrive twice. A repeated key with the same arguments returns the stored result with `replayed: true` and moves no money.

---

## Verification

```sh
uv run ruff check . && uv run pytest -q     # single gate: lint + 88 tests
uv run python scripts/inspector_check.py    # MCP Inspector CLI, 9 checks (needs Node)
```

| Check | Proves |
|---|---|
| `test_stdout_hygiene.py` | A raw session, including a forced fault, puts only valid JSON-RPC on stdout and the traceback only on stderr |
| `test_conformance.py` | Each failure returns its error code (asserted on the code, not the text), and success results match `outputSchema` |
| `test_idempotency.py` | A timed-out call retried with the same key refunds once, and the key survives a process restart |
| `test_stdio_integration.py` | The real `server.py` subprocess works under the SDK client in both `auto` and `legacy` modes |
| `test_models.py` | Each argument type is checked at its boundary values |
| `inspector_check.py` | Both tools behave correctly on good and bad input through MCP Inspector |
| ruff `T20` | There is no `print()` in the repo |

On Windows, SDK 2.3.0 writes `\r\n` line endings. Every line still parses, so the strict LF check (`--strict-lf`) only runs on other platforms.

---

## Project layout

```
server.py               entry point: stderr logging, then run the stdio server
refund_server/
  models.py             Pydantic argument types and output models
  errors.py             error codes and isError payload format
  store.py              customer fixture and SQLite refund ledger
  tools.py              RefundMCPServer and the two tools
  logging_setup.py      single stderr log handler
data/customers.json     seed customers
scripts/
  validate_stdout.py    JSON-RPC 2.0 stdout validator
  inspector_check.py    MCP Inspector CLI checks
tests/                  pytest suite
```

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `REFUND_DB_PATH` | `.state/refunds.db` | SQLite refund and idempotency ledger |
| `REFUND_CUSTOMERS_PATH` | `data/customers.json` | Customer fixture |
| `LOG_LEVEL` | `INFO` | Log level (stderr) |

MCP Inspector does not forward the parent environment, so pass `-e REFUND_DB_PATH=...` explicitly.
