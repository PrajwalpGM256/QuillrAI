"""Cancellation isolation on the timeout race.

The primary is cancelled AND awaited before the secondary starts: by the time the secondary receives
the request, the primary task is done (cancelled, not merely timed out) and the primary provider has
observed the cancellation. Nothing named upstream-primary is left running afterwards.
"""

from __future__ import annotations

import asyncio

import pytest

from llm_router.app import tenant_id
from tests.helpers import KEY_A, body_with_tokens

pytestmark = pytest.mark.anyio


def primary_tasks():
    return [t for t in asyncio.all_tasks() if t.get_name() == "upstream-primary" and not t.done()]


async def test_primary_is_done_before_the_secondary_starts(stack) -> None:
    stack.set("primary", delay_ms=5000)  # far past the 200 ms route timeout
    seen_by_secondary: dict[str, object] = {}
    secondary_app = stack.secondary

    async def watching_secondary(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/v1/chat/completions":
            primary_task = next(task for event, task in stack.events if event == "primary_start")
            seen_by_secondary["done"] = primary_task.done()
            seen_by_secondary["cancelled"] = primary_task.cancelled()
            seen_by_secondary["provider_saw_cancel"] = stack.stats("primary").cancelled
            seen_by_secondary["still_running"] = len(primary_tasks())
        await secondary_app(scope, receive, send)

    stack.transport.transports["secondary"] = type(stack.transport.transports["secondary"])(
        app=watching_secondary
    )

    r = await stack.chat(body_with_tokens(100))

    assert r.status_code == 200 and r.json()["id"] == "chatcmpl-secondary"
    assert seen_by_secondary == {
        "done": True,
        "cancelled": True,
        "provider_saw_cancel": 1,
        "still_running": 0,
    }
    assert [e for e, _ in stack.events] == [
        "primary_start",
        "primary_cancelled",
        "secondary_start",
        "secondary_done",
    ]
    assert stack.stats("primary").completed == 0  # the abandoned primary never produced a completion
    assert primary_tasks() == []


async def test_client_cancellation_cancels_primary_and_releases_reservation(stack) -> None:
    stack.set("primary", delay_ms=5000)
    request = asyncio.create_task(stack.chat(body_with_tokens(400)))

    for _ in range(200):  # wait until the primary call is in flight
        if any(e == "primary_start" for e, _ in stack.events):
            break
        await asyncio.sleep(0.005)
    assert await stack.limiter.window_tokens(tenant_id(KEY_A)) == 400  # reserved

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    primary_task = next(task for event, task in stack.events if event == "primary_start")
    assert primary_task.done() and primary_task.cancelled()
    assert ("primary_cancelled", primary_task) in stack.events
    assert stack.stats("secondary").requests == 0  # a cancelled request never fails over
    for _ in range(100):  # the shielded release finishes on the DB thread
        if await stack.limiter.window_tokens(tenant_id(KEY_A)) == 0:
            break
        await asyncio.sleep(0.01)
    assert await stack.limiter.window_tokens(tenant_id(KEY_A)) == 0
    assert primary_tasks() == []


async def test_no_orphan_tasks_after_a_burst_of_timeouts(stack) -> None:
    stack.set("primary", delay_ms=5000)
    responses = await asyncio.gather(*(stack.chat(body_with_tokens(50)) for _ in range(20)))
    assert all(r.status_code == 200 for r in responses)
    assert stack.stats("primary").cancelled == 20 and stack.stats("primary").completed == 0
    assert primary_tasks() == []
