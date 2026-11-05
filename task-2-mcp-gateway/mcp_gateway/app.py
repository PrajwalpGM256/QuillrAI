"""The gateway ASGI app: one POST /mcp endpoint, one pipeline, one audit record per request.

Pipeline (each failure returns immediately and is never forwarded):
  1. Origin            -> 403
  2. Body size / type  -> 413 / 415
  3. Authenticate      -> 401           (before the body is parsed)
  4. Parse envelope    -> 400 -32700 / -32600 / -32602
  5. decide()          -> 403 -32001 / 404 -32601 / 400 -32602
  6. Header/body check -> 400 -32020    (allowed requests only)
  7. Forward with the gateway's own credential
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_gateway import jsonrpc
from mcp_gateway.audit import AuditRecord
from mcp_gateway.auth import AuthFailure, authenticate
from mcp_gateway.config import GatewayConfig
from mcp_gateway.policy import DenyKind, Role, decide
from mcp_gateway.proxy import Downstream, DownstreamTimeout, DownstreamUnavailable

logger = logging.getLogger(__name__)

# DenyKind -> (HTTP status, JSON-RPC code, message). The policy decides; this table only renders.
DENY_RENDERING: dict[DenyKind, tuple[int, int, str]] = {
    DenyKind.UNAUTHORIZED_TOOL: (403, jsonrpc.UNAUTHORIZED, "Unauthorized Tool Call"),
    DenyKind.UNKNOWN_ROLE: (403, jsonrpc.UNAUTHORIZED, "Unauthorized"),
    DenyKind.METHOD_NOT_ALLOWED: (404, jsonrpc.METHOD_NOT_FOUND, "Method not found"),
    DenyKind.INVALID_PARAMS: (400, jsonrpc.INVALID_PARAMS, "Invalid params"),
}


def _error(
    status: int, rid: Any, code: int, message: str, reason: str, headers: dict[str, str] | None = None
):
    body = jsonrpc.error_response(rid, code, message, data={"reason": reason})
    return JSONResponse(body, status_code=status, headers=headers)


def _audit_tool(message: jsonrpc.Message) -> str | None:
    if message.method != "tools/call":
        return None
    name = message.tool_name
    return name if isinstance(name, str) else f"<{type(name).__name__}>"


def _headers_by_name(request: Request) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in request.headers.items():
        grouped.setdefault(name.lower(), []).append(value)
    return grouped


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None if it exceeds ``limit`` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def build_app(
    config: GatewayConfig, *, downstream_transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    downstream = Downstream(
        config.downstream_url,
        config.downstream_token,
        httpx2.AsyncClient(transport=downstream_transport, timeout=config.downstream_timeout),
    )

    def visible_to(role: Role):
        # tools/list filtering asks the same question as a call: "may this role call this tool?"
        return lambda tool_name: decide(role, "tools/call", tool_name).allow

    async def handle(request: Request) -> Response:
        audit = AuditRecord()
        response: Response | None = None
        try:
            response = await _pipeline(request, audit)
            return response
        finally:
            audit.http_status = response.status_code if response is not None else 500
            audit.emit()

    async def _pipeline(request: Request, audit: AuditRecord) -> Response:
        if request.method != "POST":
            audit.reason = "http_method_not_allowed"
            return Response(status_code=405, headers={"Allow": "POST"})

        origin = request.headers.get("origin")
        if origin is not None and origin not in config.allowed_origins:
            audit.reason = "origin_not_allowed"
            return _error(403, None, jsonrpc.INVALID_REQUEST, "Forbidden origin", audit.reason)

        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            audit.reason = "unsupported_media_type"
            return _error(
                415, None, jsonrpc.INVALID_REQUEST, "Content-Type must be application/json", audit.reason
            )

        raw = await _read_body(request, config.max_body_bytes)
        if raw is None:
            audit.reason = "body_too_large"
            return _error(413, None, jsonrpc.INVALID_REQUEST, "Request body too large", audit.reason)

        authn = authenticate(request.headers.getlist("authorization"), config.tokens)
        if isinstance(authn, AuthFailure):
            audit.reason, audit.token_fp = authn.reason, authn.token_fp
            challenge = 'Bearer realm="mcp-gateway"'
            if authn.reason != "missing_token":
                challenge += ', error="invalid_token"'
            return _error(
                401, None, jsonrpc.UNAUTHORIZED, "Unauthorized", authn.reason, {"WWW-Authenticate": challenge}
            )
        audit.token_fp, audit.role = authn.token_fp, authn.role.value

        message = jsonrpc.parse_message(raw)
        if isinstance(message, jsonrpc.EnvelopeError):
            audit.reason, audit.jsonrpc_id = message.reason, message.rid
            return _error(400, message.rid, message.code, message.message, message.reason)
        audit.method, audit.jsonrpc_id, audit.tool = message.method, message.id, _audit_tool(message)

        decision = decide(authn.role, message.method, message.tool_name)
        audit.reason = decision.reason
        if not decision.allow:
            assert decision.kind is not None
            status, code, text = DENY_RENDERING[decision.kind]
            return _error(status, message.id, code, text, decision.reason)

        mismatch = jsonrpc.header_mismatch(_headers_by_name(request), message)
        if mismatch is not None:
            audit.reason = mismatch
            return _error(400, message.id, jsonrpc.HEADER_MISMATCH, "Header mismatch", mismatch)

        list_filter = visible_to(authn.role) if message.method == "tools/list" else None
        try:
            response = await downstream.forward(raw, list(request.headers.items()), list_filter=list_filter)
        except DownstreamTimeout:
            audit.reason = "downstream_timeout"
            return _error(504, message.id, jsonrpc.INTERNAL_ERROR, "Downstream timeout", audit.reason)
        except DownstreamUnavailable:
            audit.reason = "downstream_unavailable"
            return _error(502, message.id, jsonrpc.INTERNAL_ERROR, "Downstream unavailable", audit.reason)
        audit.outcome = "allow"
        audit.downstream_status = response.status_code
        return response

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await downstream.aclose()

    app = Starlette(routes=[Route("/mcp", handle, methods=["POST", "GET", "DELETE"])], lifespan=lifespan)
    app.state.downstream = downstream
    return app
