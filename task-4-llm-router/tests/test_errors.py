"""One error schema for every failure, with nothing internal leaking.

Every failure mode must produce exactly {"error": {type, code, message, request_id}} (all strings,
request_id equal to X-Request-ID), and neither the body nor any header may contain an upstream URL or
host, a model name, an upstream credential, the tenant key, a traceback, or the mock's body canary.
"""

from __future__ import annotations

import json
import logging
import re

import pytest

from llm_router.limiter import LimiterUnavailable
from mock_provider.server import CANARY
from tests.helpers import (
    KEY_A,
    KEY_B,
    PRIMARY_MODEL,
    PRIMARY_UPSTREAM_KEY,
    SECONDARY_MODEL,
    SECONDARY_UPSTREAM_KEY,
    body_with_tokens,
)

pytestmark = pytest.mark.anyio

FORBIDDEN = [
    "http://primary",
    "http://secondary",
    "primary/v1",
    "secondary/v1",
    PRIMARY_MODEL,
    SECONDARY_MODEL,
    PRIMARY_UPSTREAM_KEY,
    SECONDARY_UPSTREAM_KEY,
    KEY_A,
    KEY_B,
    CANARY,
    "Traceback",
    'File "',
    "sqlite",
    "RuntimeError",
    "LimiterUnavailable",
    "client-choice",
]


async def fill_window(stack) -> None:
    stack.set("primary", usage_total=50_000)
    assert (await stack.chat(body_with_tokens(1000))).status_code == 200


async def internal_exception(stack) -> None:
    async def boom(_body):
        raise RuntimeError("secret detail http://primary/v1 sqlite at C:\\db")

    stack.router.complete = boom


async def db_unavailable(stack) -> None:
    async def down(*_args, **_kwargs):
        raise LimiterUnavailable("disk I/O error in sqlite file C:\\state\\router.db")

    stack.limiter.reserve = down


def setup(**kw):
    return kw


SCENARIOS = {
    "missing_key": (setup(key=None), 401, "missing_api_key"),
    "unknown_key": (setup(key="not-a-tenant"), 401, "invalid_api_key"),
    "basic_scheme": (setup(raw_auth="Basic dXNlcjpwYXNz"), 401, "invalid_api_key"),
    "malformed_json": (setup(raw=b"{not json"), 400, "malformed_json"),
    "json_array": (setup(raw=b"[1,2]"), 400, "malformed_json"),
    "no_messages": (setup(body={"model": "x"}), 400, "invalid_messages"),
    "stream": (setup(body=body_with_tokens(10, stream=True)), 400, "streaming_not_supported"),
    "over_budget": (setup(body=body_with_tokens(50_001)), 400, "token_budget_exceeded"),
    "rate_limited": (setup(prepare=fill_window), 429, "rate_limited"),
    "primary_400": (setup(primary={"status": 400}), 400, "upstream_rejected_request"),
    "primary_500": (setup(primary={"status": 500}), 502, "upstream_error"),
    "primary_malformed": (setup(primary={"malformed": True}), 502, "upstream_error"),
    "primary_down": (setup(down="primary"), 502, "upstream_error"),
    "both_429": (setup(primary={"status": 429}, secondary={"status": 429}), 503, "upstream_rate_limited"),
    "both_timeout": (
        setup(primary={"delay_ms": 2000}, secondary={"delay_ms": 2000}),
        503,
        "upstream_timeout",
    ),
    "secondary_500": (setup(primary={"status": 429}, secondary={"status": 500}), 503, "upstream_unavailable"),
    "secondary_malformed": (
        setup(primary={"delay_ms": 2000}, secondary={"malformed": True}),
        503,
        "upstream_unavailable",
    ),
    "secondary_down": (setup(primary={"status": 429}, down="secondary"), 503, "upstream_unavailable"),
    "internal_exception": (setup(prepare=internal_exception), 500, "internal_error"),
    "db_unavailable": (setup(prepare=db_unavailable), 503, "limiter_unavailable"),
    "unknown_path": (setup(path="/v1/models"), 404, "not_found"),
    "wrong_method": (setup(method="GET"), 405, "method_not_allowed"),
}


def assert_schema(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"type", "code", "message", "request_id"}
    assert all(isinstance(v, str) and v for v in error.values())
    assert re.fullmatch(r"[0-9a-f]{32}", error["request_id"])
    assert response.headers["x-request-id"] == error["request_id"]
    assert response.headers["content-type"].startswith("application/json")
    return error


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_every_failure_is_sanitized(stack, caplog, name: str) -> None:
    spec, status, code = SCENARIOS[name]
    caplog.set_level(logging.DEBUG)
    if "prepare" in spec:
        await spec["prepare"](stack)
    if "primary" in spec:
        stack.set("primary", **spec["primary"])
    if "secondary" in spec:
        stack.set("secondary", **spec["secondary"])
    if "down" in spec:
        stack.transport.down.add(spec["down"])

    headers = {"content-type": "application/json"}
    if "raw_auth" in spec:
        headers["authorization"] = spec["raw_auth"]
    elif spec.get("key", KEY_A) is not None:
        headers["authorization"] = f"Bearer {spec.get('key', KEY_A)}"
    content = spec.get("raw") or json.dumps(spec.get("body") or body_with_tokens(100)).encode()
    response = await stack.client.request(
        spec.get("method", "POST"), spec.get("path", "/v1/chat/completions"), content=content, headers=headers
    )

    assert response.status_code == status
    error = assert_schema(response)
    assert error["code"] == code
    exposed = response.text + json.dumps(dict(response.headers))
    for needle in FORBIDDEN:
        assert needle not in exposed, f"{needle!r} leaked in {name}"
    if code == "rate_limited":
        assert int(response.headers["retry-after"]) >= 1
    for key in (KEY_A, KEY_B):
        assert key not in caplog.text, "a tenant key reached the logs"


def test_catalog_messages_are_fixed_strings() -> None:
    from llm_router.errors import CATALOG

    for code, (status, kind, message) in CATALOG.items():
        assert 400 <= status <= 599 and kind.endswith("_error") and message.endswith(".")
        assert "{" not in message and "%" not in message, code
