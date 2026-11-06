"""The fallback policy, row by row, with the reservation released or settled on every path."""

from __future__ import annotations

import pytest

from tests.helpers import (
    KEY_A,
    PRIMARY_MODEL,
    PRIMARY_UPSTREAM_KEY,
    SECONDARY_MODEL,
    SECONDARY_UPSTREAM_KEY,
    body_with_tokens,
)

pytestmark = pytest.mark.anyio

SLOW = 2000  # ms; the in-process route timeouts are 200 ms


async def window(stack) -> int:
    from llm_router.app import tenant_id

    return await stack.limiter.window_tokens(tenant_id(KEY_A))


async def test_primary_success_settles_actual_usage(stack) -> None:
    stack.set("primary", usage_total=321)
    r = await stack.chat(body_with_tokens(500))
    assert r.status_code == 200 and r.json()["id"] == "chatcmpl-primary"
    assert stack.stats("secondary").requests == 0
    assert await window(stack) == 321  # reservation of 500 replaced by the actual 321


async def test_missing_usage_keeps_the_estimate(stack) -> None:
    stack.set("primary", usage_total=-1)  # mock reports an invalid total
    r = await stack.chat(body_with_tokens(500))
    assert r.status_code == 200
    assert await window(stack) == 500


@pytest.mark.parametrize(
    ("primary", "trigger"),
    [({"status": 429}, "429"), ({"delay_ms": SLOW}, "timeout")],
)
async def test_429_and_timeout_fail_over(stack, primary, trigger) -> None:
    stack.set("primary", **primary)
    stack.set("secondary", usage_total=77)
    r = await stack.chat(body_with_tokens(300))
    assert r.status_code == 200 and r.json()["id"] == "chatcmpl-secondary", trigger
    assert stack.stats("primary").requests == 1 and stack.stats("secondary").requests == 1
    assert await window(stack) == 77


@pytest.mark.parametrize(
    ("primary", "status", "code"),
    [
        ({"status": 400}, 400, "upstream_rejected_request"),
        ({"status": 404}, 400, "upstream_rejected_request"),
        ({"status": 500}, 502, "upstream_error"),
        ({"status": 503}, 502, "upstream_error"),
        ({"malformed": True}, 502, "upstream_error"),
    ],
)
async def test_other_primary_failures_do_not_fail_over(stack, primary, status, code) -> None:
    stack.set("primary", **primary)
    r = await stack.chat(body_with_tokens(300))
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert stack.stats("secondary").requests == 0
    assert await window(stack) == 0  # reservation released


async def test_primary_connection_error_does_not_fail_over(stack) -> None:
    stack.transport.down.add("primary")
    r = await stack.chat(body_with_tokens(300))
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert stack.stats("secondary").requests == 0
    assert await window(stack) == 0


@pytest.mark.parametrize(
    ("secondary", "down", "code"),
    [
        ({"status": 429}, False, "upstream_rate_limited"),
        ({"delay_ms": SLOW}, False, "upstream_timeout"),
        ({"status": 500}, False, "upstream_unavailable"),
        ({"status": 400}, False, "upstream_unavailable"),
        ({"malformed": True}, False, "upstream_unavailable"),
        ({}, True, "upstream_unavailable"),
    ],
)
async def test_secondary_failures_are_503(stack, secondary, down, code) -> None:
    stack.set("primary", status=429)
    stack.set("secondary", **secondary)
    if down:
        stack.transport.down.add("secondary")
    r = await stack.chat(body_with_tokens(300))
    assert r.status_code == 503 and r.json()["error"]["code"] == code
    assert await window(stack) == 0


async def test_gateway_sets_model_and_credential_per_route(stack) -> None:
    stack.set("primary", status=429)
    await stack.chat(body_with_tokens(50), Cookie="session=1")
    assert stack.stats("primary").models == [PRIMARY_MODEL]
    assert stack.stats("secondary").models == [SECONDARY_MODEL]
    assert stack.stats("primary").auth == [f"Bearer {PRIMARY_UPSTREAM_KEY}"]
    assert stack.stats("secondary").auth == [f"Bearer {SECONDARY_UPSTREAM_KEY}"]
    assert KEY_A not in str(stack.stats("primary").auth + stack.stats("secondary").auth)
