"""tools/list as an authorization surface: viewers never see admin tools, over JSON or SSE."""

from __future__ import annotations

import pytest

from mcp_gateway.proxy import filter_tools_list
from tests.conftest import ADMIN_TOKEN, VIEWER_TOKEN, Stack, modern, request
from tests.jsonrpc_check import sse_messages

pytestmark = pytest.mark.anyio

ALL_TOOLS = {"get_weather", "administrator_report", "admin_delete_user", "admin_reset_cache"}


def _tool_names(response) -> set[str]:
    if response.headers["content-type"].startswith("text/event-stream"):
        [message] = [m for m in sse_messages(response.content) if "result" in m]
    else:
        message = response.json()
    return {t["name"] for t in message["result"]["tools"]}, message


@pytest.mark.parametrize("json_response", [True, False], ids=["json", "sse"])
@pytest.mark.parametrize("shape", ["modern", "legacy"])
async def test_viewer_list_hides_admin_tools(stack: Stack, json_response: bool, shape: str) -> None:
    if shape == "modern":
        body, headers = modern("tools/list")
    else:
        body, headers = request("tools/list", {}), {}
        # Legacy requests need a session on the stateful mock: initialize first.
        init = await stack.post(
            request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
                rid=0,
            ),
            token=VIEWER_TOKEN,
        )
        headers = {"Mcp-Session-Id": init.headers["mcp-session-id"], "MCP-Protocol-Version": "2025-11-25"}

    viewer = await stack.post(body, token=VIEWER_TOKEN, headers=headers)
    admin = await stack.post(body, token=ADMIN_TOKEN, headers=headers)
    assert viewer.status_code == admin.status_code == 200
    if shape == "legacy":
        # The SDK streams legacy-session responses as SSE when json_response is off.
        # 2026-07-28 requests always get JSON.
        expected = "application/json" if json_response else "text/event-stream"
        assert viewer.headers["content-type"].startswith(expected)

    viewer_names, viewer_msg = _tool_names(viewer)
    admin_names, _ = _tool_names(admin)
    assert viewer_names == {"get_weather", "administrator_report"}
    assert admin_names == ALL_TOOLS

    for response in (viewer, admin):
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["vary"] == "Authorization"
    if shape == "modern":
        assert viewer_msg["result"]["cacheScope"] == "private"


async def test_filter_drops_malformed_entries_fail_closed() -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [{"name": "get_weather"}, {"name": 5}, "admin_x", {}, {"name": "admin_x"}],
            "cacheScope": "public",
        },
    }
    out = filter_tools_list(message, visible=lambda name: not name.startswith("admin_"))
    assert out["result"]["tools"] == [{"name": "get_weather"}]
    assert out["result"]["cacheScope"] == "private"


async def test_list_filter_uses_the_policy_function(stack: Stack, monkeypatch) -> None:
    """Filtering must route through decide(): patching it changes what the viewer sees."""
    import mcp_gateway.app as app_module
    from mcp_gateway.policy import Decision

    def patched(role, method, tool=None):
        # Allow every method, but pretend get_weather is forbidden.
        return Decision(allow=not (method == "tools/call" and tool == "get_weather"), reason="patched")

    monkeypatch.setattr(app_module, "decide", patched)
    body, headers = modern("tools/list")
    names, _ = _tool_names(await stack.post(body, token=VIEWER_TOKEN, headers=headers))
    assert "get_weather" not in names


async def test_sse_rewrite_handles_split_chunks_crlf_and_other_events() -> None:
    """Deterministic SSE path: a downstream that streams a progress event plus a tools/list result
    split across arbitrary chunk boundaries with CRLF line endings."""
    import json

    import httpx2

    from mcp_gateway.app import build_app
    from tests.conftest import make_config

    progress = {
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {"progressToken": 1, "progress": 1},
    }
    result = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"tools": [{"name": "get_weather"}, {"name": "admin_reset_cache"}], "cacheScope": "public"},
    }
    crlf = "\r\n"
    stream = (
        f"event: message{crlf}data: {json.dumps(progress)}{crlf}{crlf}"
        f"id: 42{crlf}event: message{crlf}data: {json.dumps(result)}{crlf}{crlf}"
    ).encode()
    chunks = [stream[i : i + 13] for i in range(0, len(stream), 13)]

    class ChunkedStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for c in chunks:
                yield c

    def downstream(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkedStream())

    gateway = build_app(make_config(), downstream_transport=httpx2.MockTransport(downstream))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=gateway), base_url="http://gw"
    ) as client:
        r = await client.post(
            "/mcp",
            json=request("tools/list", {}, rid=7),
            headers={"Authorization": f"Bearer {VIEWER_TOKEN}", "Content-Type": "application/json"},
        )
    await gateway.state.downstream.aclose()

    messages = sse_messages(r.content)
    assert messages[0] == progress
    assert [t["name"] for t in messages[1]["result"]["tools"]] == ["get_weather"]
    assert messages[1]["result"]["cacheScope"] == "private"
    assert b"id: 42" in r.content  # non-data fields of the rewritten event are preserved
    assert r.headers["x-accel-buffering"] == "no"
