"""The response validator is wired in, and it rejects what it claims to reject.

Every gateway response in the suite passes through ``tests.jsonrpc_check.validate_http_body`` via an
httpx2 response hook (see conftest). These tests prove the hook runs and that the validator fails
on malformed JSON-RPC, so a passing suite means every emitted response, including every -32001,
was checked.
"""

from __future__ import annotations

import jsonschema
import pytest

from tests.conftest import VIEWER_TOKEN, Stack, request
from tests.jsonrpc_check import validate_http_body, validate_message

pytestmark = pytest.mark.anyio


async def test_hook_validates_every_response_including_32001(stack: Stack) -> None:
    r = await stack.post(request("tools/call", {"name": "admin_reset_cache"}), token=VIEWER_TOKEN)
    assert r.json()["error"]["code"] == -32001
    r = await stack.post(request("tools/list", {}), token=None)
    assert r.json()["error"]["code"] == -32001
    assert stack.validated == [1, 1]


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": 1, "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "-32001", "message": "x"}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        [{"jsonrpc": "2.0", "id": 1, "result": {}}],
    ],
)
def test_validator_rejects_malformed(bad) -> None:
    with pytest.raises((jsonschema.ValidationError, AssertionError, ValueError)):
        validate_message(bad)


def test_validator_rejects_wrong_body_framing() -> None:
    with pytest.raises(AssertionError):
        validate_http_body(202, "application/json", b'{"jsonrpc":"2.0","id":1,"result":{}}')
    with pytest.raises(AssertionError):
        validate_http_body(200, "text/html", b"<html>")
    with pytest.raises(AssertionError):
        validate_http_body(200, "text/event-stream", b": keepalive\n\n")
