# mcp-gateway

An HTTP reverse proxy in front of an MCP server that authenticates a Bearer token, authorizes every JSON-RPC message in one policy function, and fails closed.

---

## What it does

- Maps the client's `Authorization: Bearer <token>` to a role (`admin` or `viewer`) through a static table, then authorizes the message body against that role.
- Blocks `tools/call` on `admin_*` tools for non-admins with `-32001 Unauthorized Tool Call`, without contacting the downstream. Viewers also never see `admin_*` tools in `tools/list`.
- Reaches the downstream with its own credential. The client's token is never forwarded, and every request writes one audit record.

---

## Quickstart

```sh
uv sync                                  # Python 3.13, pinned in uv.lock
uv run python scripts/run_stack.py       # mock downstream on :9000, gateway on :8080, audit log on this console
```

Demo tokens are `admin-demo-token` and `viewer-demo-token`. Run the commands below from Git Bash; PowerShell 5.1 strips the inner quotes from the JSON.

```sh
# viewer lists tools: 200, no admin_* tools
curl -s localhost:8080/mcp -H "Authorization: Bearer viewer-demo-token" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: 2026-07-28" -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'

# viewer calls an admin tool: 403, -32001, never forwarded
curl -s localhost:8080/mcp -H "Authorization: Bearer viewer-demo-token" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_cache","arguments":{}}}'

# no token: 401, -32001
curl -s localhost:8080/mcp -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":3,"method":"tools/list"}'
```

---

## Policy

All allow/deny outcomes come from `decide(role, method, tool_name)` in `mcp_gateway/policy.py`. Anything not listed as allowed is denied.

| Request | admin | viewer |
|---|---|---|
| `server/discover`, `initialize`, `notifications/initialized`, `ping` | forward | forward |
| `tools/list` | forward, full list | forward, `admin_*` tools removed |
| `tools/call`, non-admin tool (`get_weather`, `administrator_report`) | forward | forward |
| `tools/call`, admin tool (`admin_x`, `ADMIN_x`, `ａｄｍｉｎ_x`) | forward | 403 `-32001` |
| `tools/call`, `params.name` missing, non-string, or empty | 400 `-32602` | 400 `-32602` |
| Any other method (`resources/read`, `tools/CALL`, ...) | 404 `-32601` | 404 `-32601` |

An admin tool is any name that starts with `admin_` after NFKC normalization and case folding.

---

## Error model

Checks run in this order. The first failure returns immediately, writes the audit record, and nothing is forwarded.

| # | Failure | HTTP | JSON-RPC code |
|---|---|---|---|
| 1 | GET or DELETE | 405 | |
| 2 | `Origin` present and not allowlisted | 403 | `-32600` |
| 3 | Content type not `application/json` / body over `MAX_BODY_BYTES` | 415 / 413 | `-32600` |
| 4 | `Authorization` missing, malformed, unknown, or sent twice | 401 + `WWW-Authenticate: Bearer` | `-32001` `Unauthorized` |
| 5 | Malformed JSON, invalid UTF-8, `NaN` | 400 | `-32700` |
| 5 | Batch array, non-object, bad `jsonrpc` or `id`, client-sent response, duplicate JSON keys | 400 | `-32600` |
| 5 | `params` not an object | 400 | `-32602` |
| 6 | Policy denial | per the Policy table | |
| 7 | `Mcp-Method`, `Mcp-Name`, or `MCP-Protocol-Version` header contradicts the body | 400 | `-32020` |
| 8 | Downstream unreachable / timed out | 502 / 504 | `-32603`, no internal detail |

Every error carries `error.data.reason`, a stable string that matches the audit record's `reason`.

**The `-32001` overlap.** The brief mandates `-32001` for unauthorized requests and the Python MCP SDK uses `-32001` for `REQUEST_TIMEOUT`, a known collision followed deliberately (the code is legal, since MCP reserves `-32000..-32019` for implementations), so clients should branch on the HTTP 401/403 status.

---

## Security decisions

| Decision | Choice | Why |
|---|---|---|
| No token passthrough | The outbound request carries only `Authorization: Bearer $DOWNSTREAM_TOKEN` plus allowlisted MCP headers. Config refuses a downstream token equal to any client token | [Security Best Practices](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices#token-passthrough): servers "**MUST NOT** accept any tokens that were not explicitly issued for the MCP server". Forwarding a client token creates a [confused deputy](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices#confused-deputy-problem) |
| Fail closed | Authenticate before parsing. Unknown methods, bad shapes, and unparseable bodies are denied. An audit record defaults to `deny`. Invalid config exits 2 before binding a port | An unanticipated input must never be the path that reaches the downstream |
| Single policy point | `decide()` computes every allow/deny. `app.py` only maps a decision to a status and code. A test asserts `admin_` appears in no other gateway module | One function to review. No second prefix check can drift out of sync |
| Batches rejected | Any array body gets 400 `-32600` | Batching was removed in [2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/changelog) (PR #416), so rejecting it closes the hide-a-call-in-a-batch route at no cost |
| Prefix normalization | NFKC, then case fold, then `startswith("admin_")` | `ADMIN_x` and full-width `ａｄｍｉｎ_x` stay denied even if the downstream matches names loosely |
| Viewer `tools/list` | Filtered for JSON and SSE bodies by asking `decide(role, "tools/call", name)` per tool. `cacheScope` forced to `private`, plus `Cache-Control: private, no-store` and `Vary: Authorization` | The model never sees tools it cannot call, and a cache cannot serve an admin list to a viewer |
| Body over headers | Policy reads only the body. On allowed requests, mirrored headers that disagree with the body are rejected | A header-routing component cannot be shown a different tool than the one authorized ([Streamable HTTP: Server Validation](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http#server-validation)) |

**Residual risk.** Legacy `Mcp-Session-Id` values are not bound to a principal, which is not an authorization gap because every request is re-authenticated and re-authorized, so a session ID grants nothing at the gateway.

---

## Verification

```sh
uv run ruff check . && uv run pytest -q     # single gate: lint + 352 tests
```

| Test file | Proves |
|---|---|
| `test_matrix.py` | 11 token states x 20 method/tool cases against a hand-written oracle: status, code, zero downstream calls on deny, one audit record. `test_valid_request_without_token_is_401` pins that passing the Origin and body checks never skips auth |
| `test_policy.py` | `decide()` unit table with prefix traps, and the check that `admin_` lives only in `policy.py` |
| `test_no_passthrough.py` | The downstream sees only the gateway token, hostile headers are dropped, tokens never reach logs, and the downstream rejects a client token sent directly |
| `test_batch.py` | Mixed, empty, single, and nested batches, as admin and viewer: always 400 `-32600`, never forwarded |
| `test_list_filter.py` | Viewer lists hide admin tools over JSON and SSE (including chunked CRLF SSE), with private caching headers, routed through `decide()` |
| `test_headers_transport.py` | Header smuggling both ways, Base64 header decoding, malformed envelopes, and the 403/413/415/405/502/504 paths |
| `test_jsonrpc_conformance.py` | A response hook validates every gateway response in the suite, including every `-32001`, against a JSON-RPC 2.0 schema and the SDK's `JSONRPCMessage` |
| `test_interop.py` | The real SDK `Client`, in `auto` and `legacy` modes, through a real gateway process to a real downstream: filtered list, allowed call, `-32001` on an admin tool |
| `test_config.py` | Invalid configuration refuses to start |

---

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `GATEWAY_TOKENS` | required | JSON `{"<token>": "admin" \| "viewer"}` |
| `DOWNSTREAM_URL` | required | Downstream MCP endpoint (http/https) |
| `DOWNSTREAM_TOKEN` | required | Gateway's own downstream credential, must differ from every client token |
| `GATEWAY_ALLOWED_ORIGINS` | empty (any `Origin` is rejected) | Comma-separated allowed `Origin` values |
| `MAX_BODY_BYTES` | `1048576` | Request body limit |
| `DOWNSTREAM_TIMEOUT` | `30` | Seconds |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | Bind address |
| `LOG_LEVEL` | `INFO` | stderr log level |

---

## Project layout

```
gateway.py               entry point: load config (exit 2 if invalid), serve on 127.0.0.1
mcp_gateway/
  policy.py              decide(): the single policy decision point
  auth.py                Bearer parsing, constant-time token lookup, fingerprints
  jsonrpc.py             strict envelope parsing, error builders, header/body checks
  proxy.py               outbound header allowlist, forwarding, tools/list rewriting (JSON and SSE)
  app.py                 POST /mcp pipeline, one audit record per request
  audit.py               structured audit records (token fingerprint, never the token)
  config.py              environment config, validated at startup
  logging_setup.py       single stderr log handler
mock_downstream/
  server.py              SDK MCPServer over Streamable HTTP, accepts only DOWNSTREAM_TOKEN
  spy.py                 ASGI middleware that records every downstream request
scripts/run_stack.py     run downstream and gateway locally
tests/                   pytest suite
```
