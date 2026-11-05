"""Batches are rejected whole: no privileged call can be smuggled inside an array.

JSON-RPC batching was removed from MCP in 2025-06-18 (PR #416); 2026-07-28 Streamable HTTP says a
POST body MUST be a single JSON-RPC request or notification.
"""

from __future__ import annotations

import pytest

from tests.conftest import ADMIN_TOKEN, VIEWER_TOKEN, Stack, request

pytestmark = pytest.mark.anyio

SMUGGLE = [request("tools/list", {}, rid=1), request("tools/call", {"name": "admin_reset_cache"}, rid=2)]


@pytest.mark.parametrize("token", [VIEWER_TOKEN, ADMIN_TOKEN])
@pytest.mark.parametrize(
    "batch",
    [SMUGGLE, list(reversed(SMUGGLE)), [], [request("tools/list", {})], [SMUGGLE]],
    ids=["list-then-admin", "admin-then-list", "empty", "single-element", "nested"],
)
async def test_batches_rejected_and_never_forwarded(stack: Stack, token: str, batch: list) -> None:
    response = await stack.post(batch, token=token)
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == -32600
    assert body["id"] is None
    assert body["error"]["data"]["reason"] == "batch_not_supported"
    assert len(stack.spy.records) == 0
    assert stack.audit.records[0]["outcome"] == "deny"
