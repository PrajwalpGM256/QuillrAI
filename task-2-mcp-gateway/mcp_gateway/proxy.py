"""Forwarding to the downstream MCP server.

The gateway reaches the downstream over its own relationship: outbound requests carry the
gateway's ``DOWNSTREAM_TOKEN`` and an allowlisted set of MCP headers. The client's Authorization
header, cookies, and every other inbound header are dropped (no token passthrough).

This module holds no policy. tools/list filtering takes a ``visible(tool_name)`` predicate that
the app builds from the single policy decision point.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
from starlette.responses import Response, StreamingResponse

FORWARD_HEADERS = frozenset(
    {"content-type", "accept", "mcp-protocol-version", "mcp-method", "mcp-name", "mcp-session-id"}
)
FORWARD_HEADER_PREFIX = "mcp-param-"
RESPONSE_HEADERS = frozenset({"content-type", "mcp-session-id", "mcp-protocol-version"})
LIST_CACHE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
MAX_SSE_EVENT_BYTES = 4 * 1024 * 1024

Visible = Callable[[str], bool]


class DownstreamUnavailable(Exception):
    pass


class DownstreamTimeout(Exception):
    pass


def outbound_headers(inbound: list[tuple[str, str]], downstream_token: str) -> list[tuple[str, str]]:
    kept = [
        (name, value)
        for name, value in inbound
        if name.lower() in FORWARD_HEADERS or name.lower().startswith(FORWARD_HEADER_PREFIX)
    ]
    kept.append(("authorization", f"Bearer {downstream_token}"))
    return kept


def _response_headers(upstream: httpx2.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}


def filter_tools_list(message: Any, visible: Visible) -> Any:
    """Rewrite a tools/list JSON-RPC response: keep only visible tools, mark the result private.

    Entries that are not objects with a string ``name`` are dropped (fail closed). A list result
    varies by the caller's authorization, so a 2026-07-28 ``cacheScope`` is forced to private.
    """
    if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
        return message
    result = message["result"]
    if isinstance(result.get("tools"), list):
        result["tools"] = [
            t
            for t in result["tools"]
            if isinstance(t, dict) and isinstance(t.get("name"), str) and visible(t["name"])
        ]
    if "cacheScope" in result:
        result["cacheScope"] = "private"
    return message


async def _rewrite_sse(chunks: AsyncIterator[bytes], visible: Visible) -> AsyncIterator[bytes]:
    """Rewrite tools/list responses inside an SSE stream; pass every other event through unchanged.

    Events are delimited by a blank line (LF, CRLF, or CR line endings per the SSE spec). Each event
    is buffered whole, inspected, and re-emitted.
    """
    buffer = b""
    async for chunk in chunks:
        buffer += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        while b"\n\n" in buffer:
            event, buffer = buffer.split(b"\n\n", 1)
            yield _rewrite_event(event, visible) + b"\n\n"
        if len(buffer) > MAX_SSE_EVENT_BYTES:
            raise DownstreamUnavailable("SSE event exceeds size limit")
    if buffer.strip():
        yield _rewrite_event(buffer, visible) + b"\n\n"


def _rewrite_event(event: bytes, visible: Visible) -> bytes:
    lines = event.split(b"\n")
    data = [line[5:].removeprefix(b" ") for line in lines if line.startswith(b"data:")]
    if not data:
        return event
    try:
        message = json.loads(b"\n".join(data))
    except ValueError:
        return event
    if not (
        isinstance(message, dict) and isinstance(message.get("result"), dict) and "tools" in message["result"]
    ):
        return event
    others = [line for line in lines if not line.startswith(b"data:")]
    rewritten = json.dumps(filter_tools_list(message, visible), separators=(",", ":")).encode("utf-8")
    return b"\n".join([*others, b"data: " + rewritten])


class Downstream:
    def __init__(self, url: str, token: str, client: httpx2.AsyncClient) -> None:
        self._url = url
        self._token = token
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def forward(
        self, raw_body: bytes, inbound_headers: list[tuple[str, str]], *, list_filter: Visible | None
    ) -> Response:
        """Forward one already-authorized message. ``list_filter`` is set only for tools/list."""
        request = self._client.build_request(
            "POST", self._url, content=raw_body, headers=outbound_headers(inbound_headers, self._token)
        )
        try:
            upstream = await self._client.send(request, stream=True)
        except httpx2.TimeoutException as exc:
            raise DownstreamTimeout() from exc
        except httpx2.TransportError as exc:
            raise DownstreamUnavailable() from exc

        headers = _response_headers(upstream)
        content_type = upstream.headers.get("content-type", "")
        if list_filter is not None:
            headers.update(LIST_CACHE_HEADERS)

        if list_filter is not None and content_type.startswith("application/json") and upstream.is_success:
            try:
                body = await upstream.aread()
            finally:
                await upstream.aclose()
            try:
                rewritten = json.dumps(filter_tools_list(json.loads(body), list_filter)).encode("utf-8")
            except ValueError:
                rewritten = body
            headers.pop("content-length", None)
            return Response(rewritten, status_code=upstream.status_code, headers=headers)

        async def stream() -> AsyncIterator[bytes]:
            try:
                chunks = upstream.aiter_bytes()
                if list_filter is not None and content_type.startswith("text/event-stream"):
                    chunks = _rewrite_sse(chunks, list_filter)
                async for chunk in chunks:
                    yield chunk
            finally:
                await upstream.aclose()

        if content_type.startswith("text/event-stream"):
            headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
