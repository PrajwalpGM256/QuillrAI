"""Header/body consistency and HTTP-level transport hygiene."""

from __future__ import annotations

import base64

import httpx2
import pytest

from mcp_gateway.app import build_app
from tests.conftest import ADMIN_TOKEN, ALLOWED_ORIGIN, VIEWER_TOKEN, Stack, make_config, modern, request

pytestmark = pytest.mark.anyio


def b64(value: str) -> str:
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


# --- Mcp-Method / Mcp-Name smuggling ------------------------------------------------------------


async def test_header_says_safe_body_says_admin_viewer_denied_by_policy(stack: Stack) -> None:
    body, headers = modern("tools/call", {"name": "admin_reset_cache", "arguments": {}})
    headers["Mcp-Name"] = "get_weather"  # a header-routing component would see a safe call
    r = await stack.post(body, token=VIEWER_TOKEN, headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == -32001  # policy reads the body
    assert len(stack.spy.records) == 0


async def test_header_says_admin_body_says_safe_rejected_as_mismatch(stack: Stack) -> None:
    body, headers = modern("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}})
    headers["Mcp-Name"] = "admin_reset_cache"
    r = await stack.post(body, token=ADMIN_TOKEN, headers=headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    assert r.json()["error"]["data"]["reason"] == "mcp_name_mismatch"
    assert len(stack.spy.records) == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda h: h.__setitem__("Mcp-Method", "tools/list"), "mcp_method_mismatch"),
        (lambda h: h.pop("Mcp-Method"), "mcp_method_missing"),
        (lambda h: h.pop("Mcp-Name"), "mcp_name_missing"),
        (lambda h: h.__setitem__("Mcp-Name", "=?base64?!!notbase64!!?="), "mcp_name_mismatch"),
        (lambda h: h.__setitem__("MCP-Protocol-Version", "2027-01-01"), "protocol_version_mismatch"),
    ],
)
async def test_header_validation_failures(stack: Stack, mutate, reason: str) -> None:
    body, headers = modern("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}})
    mutate(headers)
    r = await stack.post(body, token=ADMIN_TOKEN, headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020
    assert r.json()["error"]["data"]["reason"] == reason
    assert len(stack.spy.records) == 0


async def test_repeated_mcp_header_rejected(stack: Stack) -> None:
    body, headers = modern("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}})
    r = await stack.post(body, headers=[*headers.items(), ("Mcp-Name", "admin_reset_cache")])
    assert r.status_code == 400 and r.json()["error"]["data"]["reason"] == "repeated_mcp_header"
    assert len(stack.spy.records) == 0


async def test_base64_sentinel_name_accepted_when_it_matches(stack: Stack) -> None:
    body, headers = modern("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}})
    headers["Mcp-Name"] = b64("get_weather")
    r = await stack.post(body, headers=headers)
    assert r.status_code == 200 and len(stack.spy.records) == 1


async def test_legacy_request_without_headers_is_not_header_checked(stack: Stack) -> None:
    r = await stack.post(request("ping"), headers={"MCP-Protocol-Version": "2025-11-25"})
    assert len(stack.spy.records) == 1  # forwarded; 2025-11-25 does not define Mcp-Method
    assert stack.audit.records[0]["outcome"] == "allow"
    assert r.status_code != 403


# --- envelope edge cases -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code", "reason"),
    [
        (b"{not json", -32700, "malformed_json"),
        (b"", -32700, "malformed_json"),
        (b"\xff\xfe", -32700, "malformed_json"),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"x":NaN}}', -32700, "malformed_json"),
        (b'"tools/list"', -32600, "not_an_object"),
        (b'{"jsonrpc":"1.0","id":1,"method":"tools/list"}', -32600, "bad_jsonrpc_version"),
        (b'{"jsonrpc":"2.0","id":1}', -32600, "missing_method"),
        (b'{"jsonrpc":"2.0","id":1,"result":{}}', -32600, "client_response_not_allowed"),
        (b'{"jsonrpc":"2.0","id":1,"method":7}', -32600, "method_not_string"),
        (b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600, "bad_id"),
        (b'{"jsonrpc":"2.0","id":null,"method":"tools/list"}', -32600, "bad_id"),
        (b'{"jsonrpc":"2.0","id":1.5,"method":"tools/list"}', -32600, "bad_id"),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":"x"}', -32602, "params_not_object"),
        (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_weather","name":"admin_reset_cache"}}',
            -32600,
            "duplicate_json_key",
        ),
    ],
)
async def test_malformed_envelopes_never_forwarded(stack: Stack, raw: bytes, code: int, reason: str) -> None:
    r = await stack.post(None, raw=raw)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code
    assert r.json()["error"]["data"]["reason"] == reason
    assert len(stack.spy.records) == 0
    assert stack.audit.records[0]["outcome"] == "deny"


# --- HTTP-level hygiene ------------------------------------------------------------------------


async def test_disallowed_origin_403_allowed_origin_passes(stack: Stack) -> None:
    body, headers = modern("tools/list")
    bad = await stack.post(body, headers={**headers, "Origin": "https://evil.example"})
    assert bad.status_code == 403 and len(stack.spy.records) == 0
    good = await stack.post(body, headers={**headers, "Origin": ALLOWED_ORIGIN})
    assert good.status_code == 200 and len(stack.spy.records) == 1


async def test_oversized_body_413(stack: Stack) -> None:
    big = request("tools/call", {"name": "get_weather", "arguments": {"city": "x" * 2_000_000}})
    r = await stack.post(big)
    assert r.status_code == 413 and len(stack.spy.records) == 0


async def test_wrong_content_type_415(stack: Stack) -> None:
    r = await stack.client.post(
        "/mcp",
        content=b"{}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "text/plain"},
    )
    assert r.status_code == 415 and len(stack.spy.records) == 0


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_get_and_delete_405(stack: Stack, method: str) -> None:
    r = await stack.client.request(method, "/mcp", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert len(stack.spy.records) == 0


async def test_allowed_notification_forwarded(stack: Stack) -> None:
    await stack.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert len(stack.spy.records) == 1


async def test_unreachable_downstream_502_and_timeout_504() -> None:
    def refuse(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    def slow(_req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    for handler, status in ((refuse, 502), (slow, 504)):
        gateway = build_app(make_config(), downstream_transport=httpx2.MockTransport(handler))
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway), base_url="http://gw") as c:
            r = await c.post(
                "/mcp",
                json=request("ping"),
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"},
            )
        await gateway.state.downstream.aclose()
        assert r.status_code == status
        assert r.json()["error"]["code"] == -32603
        assert "refused" not in r.text and "slow" not in r.text  # no internal detail leaks
