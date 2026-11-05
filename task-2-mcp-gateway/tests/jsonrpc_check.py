"""Independent JSON-RPC 2.0 validation for every response the gateway emits.

Three checks, so no single implementation is trusted:
  1. a hand-written JSON Schema (draft 2020-12) for JSON-RPC 2.0 messages, run with jsonschema;
  2. a structural check written from the JSON-RPC 2.0 specification;
  3. the MCP SDK's own ``JSONRPCMessage`` type.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
from mcp_types import jsonrpc_message_adapter

_ID = {"type": ["string", "integer"]}
JSONRPC_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "oneOf": [
        {  # success response
            "type": "object",
            "required": ["jsonrpc", "id", "result"],
            "properties": {"jsonrpc": {"const": "2.0"}, "id": _ID, "result": {}},
            "not": {"required": ["error"]},
        },
        {  # error response (id is null when it could not be determined)
            "type": "object",
            "required": ["jsonrpc", "id", "error"],
            "properties": {
                "jsonrpc": {"const": "2.0"},
                "id": {"type": ["string", "integer", "null"]},
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {"code": {"type": "integer"}, "message": {"type": "string"}, "data": {}},
                },
            },
            "not": {"required": ["result"]},
        },
        {  # notification (e.g. progress on an SSE stream)
            "type": "object",
            "required": ["jsonrpc", "method"],
            "properties": {
                "jsonrpc": {"const": "2.0"},
                "method": {"type": "string"},
                "params": {"type": "object"},
            },
            "not": {"anyOf": [{"required": ["id"]}, {"required": ["result"]}, {"required": ["error"]}]},
        },
    ],
}
jsonschema.Draft202012Validator.check_schema(JSONRPC_SCHEMA)
_VALIDATOR = jsonschema.Draft202012Validator(JSONRPC_SCHEMA)


def structural_problem(msg: Any) -> str | None:
    """JSON-RPC 2.0 §5 response object rules, written from the spec text."""
    if not isinstance(msg, dict):
        return "not a JSON object"
    if msg.get("jsonrpc") != "2.0":
        return 'jsonrpc must be exactly "2.0"'
    if "method" in msg:
        return None if isinstance(msg["method"], str) and "id" not in msg else "bad notification"
    if "id" not in msg:
        return "response missing id"
    if ("result" in msg) == ("error" in msg):
        return "response must have exactly one of result/error"
    if "error" in msg:
        err = msg["error"]
        if not isinstance(err, dict):
            return "error must be an object"
        if type(err.get("code")) is not int:
            return "error.code must be an integer"
        if not isinstance(err.get("message"), str):
            return "error.message must be a string"
    return None


def validate_message(msg: Any) -> None:
    _VALIDATOR.validate(msg)
    assert (problem := structural_problem(msg)) is None, problem
    jsonrpc_message_adapter.validate_python(msg)


def sse_messages(body: bytes) -> list[Any]:
    events = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n\n")
    out = []
    for event in events:
        data = [ln[5:].removeprefix(b" ") for ln in event.split(b"\n") if ln.startswith(b"data:")]
        if data:
            out.append(json.loads(b"\n".join(data)))
    return out


def validate_http_body(status: int, content_type: str, body: bytes) -> int:
    """Validate one HTTP response from the gateway. Returns how many JSON-RPC messages were checked."""
    if status in (202, 405):
        assert body == b"", f"{status} must have an empty body"
        return 0
    if content_type.startswith("text/event-stream"):
        messages = sse_messages(body)
        assert messages, "SSE response carried no JSON-RPC message"
    else:
        assert content_type.startswith("application/json"), f"unexpected content type {content_type!r}"
        messages = [json.loads(body)]
    for msg in messages:
        validate_message(msg)
    return len(messages)
