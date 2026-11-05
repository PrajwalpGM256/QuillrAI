"""JSON-RPC 2.0 envelope handling for the gateway.

Parsing is strict: anything the gateway cannot classify unambiguously is rejected rather than
forwarded, because a downstream parser that reads it differently would see a different request
than the one the gateway authorized.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNAUTHORIZED = -32001  # mandated by the task brief; see README on the SDK's REQUEST_TIMEOUT overlap
HEADER_MISMATCH = -32020  # MCP 2026-07-28, Streamable HTTP "Server Validation"

# Protocol revisions from this one onward require Mcp-Method / Mcp-Name on every request.
HEADER_VALIDATED_SINCE = "2026-07-28"
_NAMED_METHODS = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}


def error_response(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": error}


@dataclass(frozen=True)
class Message:
    body: dict[str, Any]
    method: str
    is_notification: bool

    @property
    def id(self) -> Any:
        return self.body.get("id")

    @property
    def params(self) -> dict[str, Any]:
        return self.body.get("params") or {}

    @property
    def tool_name(self) -> object:
        """``params.name`` exactly as sent (any type, or None when absent)."""
        return self.params.get("name")


@dataclass(frozen=True)
class EnvelopeError:
    code: int
    message: str
    reason: str
    rid: Any = None


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    # {"name": "safe_tool", "name": "privileged_tool"}: parsers disagree on which value wins. Reject.
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-standard JSON constant {token}")


def parse_message(raw: bytes) -> Message | EnvelopeError:
    try:
        text = raw.decode("utf-8")
        body = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except _DuplicateKey:
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "duplicate_json_key")
    except (UnicodeDecodeError, ValueError, RecursionError):
        return EnvelopeError(PARSE_ERROR, "Parse error", "malformed_json")

    if isinstance(body, list):
        # Batching was removed in MCP 2025-06-18; a POST body MUST be a single message.
        return EnvelopeError(
            INVALID_REQUEST, "Invalid Request: batches are not supported", "batch_not_supported"
        )
    if not isinstance(body, dict):
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "not_an_object")

    rid = body.get("id")
    rid_ok = "id" not in body or (isinstance(rid, str | int) and not isinstance(rid, bool))
    echo = rid if rid_ok else None
    if body.get("jsonrpc") != "2.0":
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "bad_jsonrpc_version", echo)
    if "method" not in body:
        reason = "client_response_not_allowed" if ("result" in body or "error" in body) else "missing_method"
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", reason, echo)
    if not isinstance(body["method"], str):
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "method_not_string", echo)
    if not rid_ok:
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "bad_id")
    if "result" in body or "error" in body:
        return EnvelopeError(INVALID_REQUEST, "Invalid Request", "request_with_result", echo)
    if "params" in body and not isinstance(body["params"], dict):
        return EnvelopeError(INVALID_PARAMS, "Invalid params", "params_not_object", echo)
    return Message(body=body, method=body["method"], is_notification="id" not in body)


def decode_header_value(value: str) -> str | None:
    """Decode the MCP Base64 sentinel form ``=?base64?...?=``. Returns None if malformed."""
    if value.startswith("=?base64?") and value.endswith("?=") and len(value) >= len("=?base64??="):
        try:
            return base64.b64decode(value[len("=?base64?") : -2], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return None
    return value


def _single(headers: Mapping[str, list[str]], name: str) -> str | None | bool:
    """The one value of header ``name``; None if absent; False if repeated."""
    values = headers.get(name, [])
    if len(values) > 1:
        return False
    return values[0] if values else None


def header_mismatch(headers: Mapping[str, list[str]], message: Message) -> str | None:
    """Check MCP request-metadata headers against the body. Returns a reason string on mismatch.

    Implements the 2026-07-28 Streamable HTTP "Server Validation" rule for a component that reads
    the body: mirrored headers, when present, must equal the body; on 2026-07-28+ requests the
    required ones must be present. ``Mcp-Param-*`` headers are left to the downstream, which knows
    the tool schemas; the gateway never authorizes on them.
    """
    version = _single(headers, "mcp-protocol-version")
    mcp_method = _single(headers, "mcp-method")
    mcp_name = _single(headers, "mcp-name")
    if False in (version, mcp_method, mcp_name):
        return "repeated_mcp_header"

    body_version = None
    meta = message.params.get("_meta")
    if isinstance(meta, dict):
        body_version = meta.get("io.modelcontextprotocol/protocolVersion")
    if isinstance(version, str) and body_version is not None and body_version != version:
        return "protocol_version_mismatch"

    if mcp_method is not None and mcp_method != message.method:
        return "mcp_method_mismatch"

    field_name = _NAMED_METHODS.get(message.method)
    if mcp_name is not None:
        decoded = decode_header_value(mcp_name)
        body_value = message.params.get(field_name) if field_name else None
        if decoded is None or field_name is None or decoded != body_value:
            return "mcp_name_mismatch"

    requires_headers = isinstance(version, str) and version >= HEADER_VALIDATED_SINCE
    if requires_headers and not message.is_notification:
        if mcp_method is None:
            return "mcp_method_missing"
        if field_name is not None and mcp_name is None:
            return "mcp_name_missing"
    return None
