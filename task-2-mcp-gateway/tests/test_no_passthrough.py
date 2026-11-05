"""No token passthrough: the client's credential terminates at the gateway.

MCP Security Best Practices, "Token Passthrough": MCP servers MUST NOT accept tokens not issued
for them, and forwarding a client token downstream creates a confused deputy. The downstream must
only ever see the gateway's own credential.
"""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_TOKEN, DOWNSTREAM_TOKEN, VIEWER_TOKEN, Stack, modern, request

pytestmark = pytest.mark.anyio

HOSTILE_HEADERS = [
    ("Cookie", f"session={ADMIN_TOKEN}"),
    ("Proxy-Authorization", f"Bearer {ADMIN_TOKEN}"),
    ("X-Forwarded-For", "10.0.0.1"),
    ("X-Forwarded-Authorization", f"Bearer {ADMIN_TOKEN}"),
    ("X-Api-Key", ADMIN_TOKEN),
    ("Forwarded", "for=10.0.0.1"),
]


@pytest.mark.parametrize("token", [ADMIN_TOKEN, VIEWER_TOKEN])
async def test_downstream_sees_only_gateway_credential(stack: Stack, token: str) -> None:
    body, mcp_headers = modern("tools/list")
    response = await stack.post(body, token=token, headers=[*mcp_headers.items(), *HOSTILE_HEADERS])
    assert response.status_code == 200
    assert len(stack.spy.records) == 1
    seen = stack.spy.records[0]

    assert seen.header("authorization") == [f"Bearer {DOWNSTREAM_TOKEN}"]
    for name, _ in HOSTILE_HEADERS:
        assert seen.header(name) == [], f"{name} was forwarded"
    for header_name, value in seen.headers:
        assert ADMIN_TOKEN not in value and VIEWER_TOKEN not in value, f"client token leaked in {header_name}"


async def test_only_allowlisted_headers_are_forwarded(stack: Stack) -> None:
    body, mcp_headers = modern("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}})
    await stack.post(
        body,
        headers={
            **mcp_headers,
            "Mcp-Param-Region": "eu",
            "Mcp-Session-Id": "abc",
            "User-Agent": "evil",
            "X-Custom": "1",
        },
    )
    names = {name.lower() for name, _ in stack.spy.records[0].headers}
    assert {"mcp-param-region", "mcp-session-id", "authorization", "content-type", "accept"} <= names
    assert "evil" not in stack.spy.records[0].header("user-agent")
    assert "x-custom" not in names


async def test_client_token_never_logged(stack: Stack) -> None:
    for token in (ADMIN_TOKEN, VIEWER_TOKEN, "unknown-token-xyz"):
        await stack.post(request("tools/call", {"name": "admin_reset_cache"}), token=token)
    everything = "\n".join(stack.audit.raw)
    for token in (ADMIN_TOKEN, VIEWER_TOKEN, "unknown-token-xyz"):
        assert token not in everything
    assert all(r["token_fp"] and len(r["token_fp"]) == 8 for r in stack.audit.records)


async def test_downstream_rejects_client_token_directly(stack: Stack) -> None:
    """The downstream trusts only the gateway: a client token sent straight to it is refused."""
    import httpx2

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=stack.spy), base_url="http://downstream"
    ) as c:
        r = await c.post(
            "/mcp", json=request("tools/list", {}), headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
    assert r.status_code == 401
