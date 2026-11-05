"""Authorization matrix: token state x method x tool-name shape, end to end through the gateway.

The expected outcome for each case comes from the hand-written oracle below, not from calling
``decide()``. Every case also asserts the downstream spy count (0 on deny, 1 on allow) and that
exactly one audit record was written with the right fields.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import ADMIN_TOKEN, ALLOWED_ORIGIN, VIEWER_TOKEN, Stack, request

pytestmark = pytest.mark.anyio

# token state -> (Authorization header values, resolved role or None)
TOKEN_STATES: dict[str, tuple[list[str], str | None]] = {
    "admin": ([f"Bearer {ADMIN_TOKEN}"], "admin"),
    "viewer": ([f"Bearer {VIEWER_TOKEN}"], "viewer"),
    "admin_lowercase_scheme": ([f"bearer {ADMIN_TOKEN}"], "admin"),
    "missing": ([], None),
    "empty": ([""], None),
    "basic_scheme": (["Basic YWRtaW46cHc="], None),
    "unknown_token": (["Bearer not-a-real-token"], None),
    "bearer_without_token": (["Bearer"], None),
    "bearer_double_space": ([f"Bearer  {ADMIN_TOKEN}"], None),
    "trailing_space": ([f"Bearer {ADMIN_TOKEN} "], None),
    "two_headers": ([f"Bearer {ADMIN_TOKEN}", f"Bearer {ADMIN_TOKEN}"], None),
}

ALLOW = ("allow", None, None)
DENY_TOOL = ("deny", 403, -32001)
DENY_METHOD = ("deny", 404, -32601)
DENY_PARAMS = ("deny", 400, -32602)

# (label, message, expected for admin, expected for viewer)
CASES: list[tuple[str, dict[str, Any], tuple, tuple]] = [
    ("server/discover", request("server/discover", {}), ALLOW, ALLOW),
    (
        "initialize",
        request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "m", "version": "0"},
            },
        ),
        ALLOW,
        ALLOW,
    ),
    ("notifications/initialized", {"jsonrpc": "2.0", "method": "notifications/initialized"}, ALLOW, ALLOW),
    ("ping", request("ping"), ALLOW, ALLOW),
    ("tools/list", request("tools/list", {}), ALLOW, ALLOW),
    ("resources/read", request("resources/read", {"uri": "file:///etc/passwd"}), DENY_METHOD, DENY_METHOD),
    ("tools/CALL", request("tools/CALL", {"name": "get_weather"}), DENY_METHOD, DENY_METHOD),
    ("empty method", request("", {}), DENY_METHOD, DENY_METHOD),
    (
        "unknown notification",
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
        DENY_METHOD,
        DENY_METHOD,
    ),
    (
        "call get_weather",
        request("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}}),
        ALLOW,
        ALLOW,
    ),
    ("call administrator_report", request("tools/call", {"name": "administrator_report"}), ALLOW, ALLOW),
    ("call admin_reset_cache", request("tools/call", {"name": "admin_reset_cache"}), ALLOW, DENY_TOOL),
    ("call ADMIN_reset_cache", request("tools/call", {"name": "ADMIN_reset_cache"}), ALLOW, DENY_TOOL),
    ("call full-width admin", request("tools/call", {"name": "ａｄｍｉｎ_reset_cache"}), ALLOW, DENY_TOOL),
    ("call name missing", request("tools/call", {"arguments": {}}), DENY_PARAMS, DENY_PARAMS),
    ("call name 123", request("tools/call", {"name": 123}), DENY_PARAMS, DENY_PARAMS),
    ("call name null", request("tools/call", {"name": None}), DENY_PARAMS, DENY_PARAMS),
    ("call name empty", request("tools/call", {"name": ""}), DENY_PARAMS, DENY_PARAMS),
    ("call params missing", request("tools/call"), DENY_PARAMS, DENY_PARAMS),
    ("call params array", request("tools/call", ["admin_reset_cache"]), DENY_PARAMS, DENY_PARAMS),
]


def _matrix():
    for token_label, (auth_values, role) in TOKEN_STATES.items():
        for label, message, admin_expect, viewer_expect in CASES:
            if role is None:
                expect = ("deny", 401, -32001)
            else:
                expect = admin_expect if role == "admin" else viewer_expect
            yield pytest.param(auth_values, role, message, expect, id=f"{token_label}|{label}")


@pytest.mark.parametrize(("auth_values", "role", "message", "expect"), list(_matrix()))
async def test_matrix(
    stack: Stack, auth_values: list[str], role: str | None, message: dict, expect: tuple
) -> None:
    outcome, status, code = expect
    headers = [("Authorization", v) for v in auth_values]
    response = await stack.post(message, token=None, headers=headers)

    assert len(stack.audit.records) == 1, stack.audit.records
    record = stack.audit.records[0]
    assert record["outcome"] == outcome
    assert record["role"] == role
    assert record["http_status"] == response.status_code

    if outcome == "allow":
        assert len(stack.spy.records) == 1
        assert record["method"] == message["method"]
        assert record["downstream_status"] == response.status_code
        return

    assert len(stack.spy.records) == 0, "a denied request reached the downstream"
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["data"]["reason"] == record["reason"]
    if status == 401:
        assert response.headers["www-authenticate"].startswith('Bearer realm="mcp-gateway"')
        assert record["method"] is None  # unauthenticated bodies are never parsed
    if status == 403:
        assert body["error"]["message"] == "Unauthorized Tool Call"
        assert body["id"] == message["id"]


async def test_valid_request_without_token_is_401(stack: Stack) -> None:
    """Pins pipeline order: passing Origin and body checks never lets an unauthenticated request through."""
    response = await stack.post(
        request("tools/call", {"name": "get_weather", "arguments": {"city": "Oslo"}}),
        token=None,
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32001
    assert len(stack.spy.records) == 0
    assert stack.audit.records[0]["reason"] == "missing_token"
