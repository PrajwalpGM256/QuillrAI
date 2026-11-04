"""Tool execution errors with stable, machine-readable codes.

Every tool execution error (``isError: true``) carries a single JSON text block:

    {"error": {"code": "<stable_code>", "message": "<human text>", ...code-specific fields}}

Callers branch on ``error.code``; ``message`` is for humans and may change.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

INVALID_ARGUMENTS = "invalid_arguments"
CUSTOMER_NOT_FOUND = "customer_not_found"
NOTHING_REFUNDABLE = "nothing_refundable"
AMOUNT_EXCEEDS_REFUNDABLE = "amount_exceeds_refundable"
IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"


def error_payload(code: str, message: str, **fields: Any) -> str:
    return json.dumps({"error": {"code": code, "message": message, **fields}})


class DomainError(ToolError):
    """A well-formed request that cannot proceed given current state."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        self.code = code
        self.fields = fields
        super().__init__(error_payload(code, message, **fields))


def invalid_arguments_from_validation(exc: ValidationError) -> ToolError:
    details = [
        {
            "field": ".".join(str(part) for part in err["loc"]) or "(arguments)",
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors(include_url=False, include_input=False)
    ]
    return ToolError(error_payload(INVALID_ARGUMENTS, "Arguments failed validation.", details=details))


def invalid_arguments_unexpected(fields: list[str]) -> ToolError:
    details = [{"field": f, "message": "Unexpected argument", "type": "extra_forbidden"} for f in fields]
    return ToolError(error_payload(INVALID_ARGUMENTS, "Arguments failed validation.", details=details))
