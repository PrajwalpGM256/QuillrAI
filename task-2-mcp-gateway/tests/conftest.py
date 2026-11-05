from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from mcp_gateway.app import build_app
from mcp_gateway.config import GatewayConfig
from mcp_gateway.policy import Role
from mock_downstream.server import build_downstream
from mock_downstream.spy import Spy
from tests.jsonrpc_check import validate_http_body

ADMIN_TOKEN = "admin-token-7f3a9c"
VIEWER_TOKEN = "viewer-token-2b8e41"
DOWNSTREAM_TOKEN = "gateway-to-downstream-5d1c"
TOKENS = {ADMIN_TOKEN: Role.ADMIN, VIEWER_TOKEN: Role.VIEWER}
ALLOWED_ORIGIN = "https://app.example.com"
ACCEPT = "application/json, text/event-stream"
MODERN = "2026-07-28"


class AuditCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[dict[str, Any]] = []
        self.raw: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        self.raw.append(text)
        if record.name == "mcp_gateway.audit":
            self.records.append(json.loads(text))


@dataclass
class Stack:
    client: httpx2.AsyncClient
    spy: Spy
    audit: AuditCapture
    validated: list[int] = field(default_factory=list)

    async def post(
        self,
        body: Any,
        *,
        token: str | None = ADMIN_TOKEN,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
        raw: bytes | None = None,
    ) -> httpx2.Response:
        hdrs: list[tuple[str, str]] = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
        if token is not None:
            hdrs.append(("Authorization", f"Bearer {token}"))
        if isinstance(headers, dict):
            hdrs.extend(headers.items())
        elif headers:
            hdrs.extend(headers)
        content = raw if raw is not None else json.dumps(body).encode()
        return await self.client.post("/mcp", content=content, headers=hdrs)


def request(method: str, params: Any = None, rid: Any = 1) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def modern(
    method: str, params: dict[str, Any] | None = None, rid: int = 1
) -> tuple[dict[str, Any], dict[str, str]]:
    """A 2026-07-28 request with the _meta and mirrored headers a conforming client sends."""
    p = dict(params or {})
    p["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "tests", "version": "0"},
    }
    headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if method == "tools/call":
        headers["Mcp-Name"] = p["name"]
    return request(method, p, rid), headers


def make_config(**overrides: Any) -> GatewayConfig:
    values: dict[str, Any] = {
        "tokens": TOKENS,
        "downstream_url": "http://downstream/mcp",
        "downstream_token": DOWNSTREAM_TOKEN,
        "allowed_origins": frozenset({ALLOWED_ORIGIN}),
    }
    values.update(overrides)
    return GatewayConfig(**values)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def json_response() -> bool:
    return True


@pytest.fixture
async def stack(json_response: bool) -> AsyncIterator[Stack]:
    capture = AuditCapture()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.INFO)

    spy, downstream_app = build_downstream(DOWNSTREAM_TOKEN, json_response=json_response)
    async with downstream_app.router.lifespan_context(downstream_app):
        gateway = build_app(make_config(), downstream_transport=httpx2.ASGITransport(app=spy))
        validated: list[int] = []

        async def validate(response: httpx2.Response) -> None:
            # Every response the gateway emits in the suite must be valid JSON-RPC 2.0.
            await response.aread()
            validated.append(
                validate_http_body(
                    response.status_code, response.headers.get("content-type", ""), response.content
                )
            )

        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=gateway),
            base_url="http://gateway",
            event_hooks={"response": [validate]},
        )
        try:
            yield Stack(client=client, spy=spy, audit=capture, validated=validated)
        finally:
            await client.aclose()
            await gateway.state.downstream.aclose()
            root.removeHandler(capture)
            root.setLevel(previous_level)
