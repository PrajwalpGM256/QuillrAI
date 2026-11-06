# QuillrAI assessment

Four tasks covering an MCP server, an MCP security gateway, a streaming LLM PII guardrail, and a resilient LLM router.

---

## Tasks

| Task | Folder | What it is | Stack | Run command |
|---|---|---|---|---|
| 1 | `task-1-mcp-server/` | stdio MCP server for customer lookup and idempotent refunds | Python 3.13, MCP SDK, Pydantic, SQLite | `cd task-1-mcp-server && uv run server.py` |
| 2 | `task-2-mcp-gateway/` | HTTP reverse proxy that authenticates and authorizes every JSON-RPC message before it reaches an MCP server | Python 3.13, Starlette, httpx2, uvicorn | `cd task-2-mcp-gateway && uv run python scripts/run_stack.py` |
| 3 | `task-3-llm-guardrail/` | OpenAI-compatible streaming proxy that redacts PII from SSE output, chunk-invariant | Python 3.13, Starlette, httpx2, uvicorn, `regex` | `cd task-3-llm-guardrail && uv run python scripts/run_stack.py` |
| 4 | `task-4-llm-router/` | OpenAI-compatible gateway with a per-tenant token rate limiter and primary-to-secondary failover | Python 3.13, Starlette, httpx2, uvicorn, SQLite | `cd task-4-llm-router && uv run python scripts/run_stack.py` |

Each task is a self-contained uv project with its own lockfile and README. No API keys are needed; tasks 2 to 4 run against local mock upstreams.

---

## Layout

```
README.md                this file
task-1-mcp-server/       refund MCP server: strict validation, idempotency, stdio isolation
task-2-mcp-gateway/      MCP gateway: Bearer auth, single policy point, no token passthrough
task-3-llm-guardrail/    streaming PII redaction with a derived buffer bound
task-4-llm-router/       token-aware rate limiting in SQLite, failover on 429 or timeout
```

---

## Running the tests

### task-1-mcp-server

```sh
cd task-1-mcp-server && uv run ruff check . && uv run pytest -q
```

### task-2-mcp-gateway

```sh
cd task-2-mcp-gateway && uv run ruff check . && uv run pytest -q
```

### task-3-llm-guardrail

```sh
cd task-3-llm-guardrail && uv run ruff check . && uv run pytest -q
```

### task-4-llm-router

```sh
cd task-4-llm-router && uv run ruff check . && uv run pytest -q
```

---

## Design thread

Each task is a controlled layer between an AI agent and the systems it touches, and each enforces a different kind of gate. Task 1 validates every tool input before a refund is written, task 2 authorizes every message before it reaches the server, task 3 sanitizes model output before it reaches the client, and task 4 meters and routes requests before they reach a provider. In every case the gate rejects what it cannot verify, and that behaviour is proven by tests rather than asserted.
