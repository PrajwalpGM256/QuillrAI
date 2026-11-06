"""The one error schema for every gateway failure.

    {"error": {"type": ..., "code": ..., "message": ..., "request_id": ...}}

Messages are fixed strings chosen by code. Nothing from an upstream (URL, model name, status text,
body) and nothing from an exception (text, class, traceback) is ever placed in a response.
"""

from __future__ import annotations

from dataclasses import dataclass

from starlette.responses import JSONResponse

INVALID = "invalid_request_error"
AUTH = "authentication_error"
RATE = "rate_limit_error"
UPSTREAM = "upstream_error"
INTERNAL = "internal_error"

# code -> (HTTP status, type, message)
CATALOG: dict[str, tuple[int, str, str]] = {
    "missing_api_key": (401, AUTH, "An API key is required as a Bearer token."),
    "invalid_api_key": (401, AUTH, "The API key is not valid."),
    "request_too_large": (413, INVALID, "The request body is too large."),
    "malformed_json": (400, INVALID, "The request body must be a JSON object."),
    "invalid_messages": (400, INVALID, "messages must be a non-empty list of message objects."),
    "streaming_not_supported": (400, INVALID, "Streaming is not supported by this gateway."),
    "token_budget_exceeded": (400, INVALID, "The request alone exceeds the per-window token budget."),
    "rate_limited": (
        429,
        RATE,
        "Token rate limit exceeded for this API key. Retry after the indicated delay.",
    ),
    "upstream_rejected_request": (400, UPSTREAM, "The model provider rejected the request."),
    "upstream_error": (502, UPSTREAM, "The model provider failed to complete the request."),
    "upstream_rate_limited": (503, UPSTREAM, "All model providers are rate limited. Try again later."),
    "upstream_timeout": (503, UPSTREAM, "All model providers timed out. Try again later."),
    "upstream_unavailable": (503, UPSTREAM, "All model providers failed. Try again later."),
    "limiter_unavailable": (503, INTERNAL, "Rate limiting is temporarily unavailable. Try again later."),
    "not_found": (404, INVALID, "Not found."),
    "method_not_allowed": (405, INVALID, "Method not allowed."),
    "internal_error": (500, INTERNAL, "Internal error."),
}


@dataclass
class GatewayError(Exception):
    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in CATALOG:
            raise ValueError(f"unknown error code {self.code!r}")

    @property
    def status(self) -> int:
        return CATALOG[self.code][0]


def error_body(code: str, request_id: str) -> dict[str, dict[str, str]]:
    _, kind, message = CATALOG[code]
    return {"error": {"type": kind, "code": code, "message": message, "request_id": request_id}}


def error_response(error: GatewayError, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(error_body(error.code, request_id), status_code=error.status, headers=headers)
