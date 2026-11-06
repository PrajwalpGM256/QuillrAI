"""N simultaneous requests that together exceed the limit: admitted tokens never exceed 50,000."""

from __future__ import annotations

import asyncio

import pytest

from llm_router.app import tenant_id
from tests.helpers import KEY_A, KEY_B, body_with_tokens

pytestmark = pytest.mark.anyio


async def test_120_concurrent_requests_admit_exactly_50(stack) -> None:
    # Each request reserves exactly 1,000 and the provider charges exactly 1,000, so only 50 fit.
    stack.set("primary", usage_total=1000, delay_ms=30)  # slow enough that requests overlap
    responses = await asyncio.gather(*(stack.chat(body_with_tokens(1000)) for _ in range(120)))

    ok = [r for r in responses if r.status_code == 200]
    limited = [r for r in responses if r.status_code == 429]
    assert len(ok) == 50 and len(limited) == 70
    assert all(r.json()["error"]["code"] == "rate_limited" for r in limited)
    assert all(int(r.headers["retry-after"]) >= 1 for r in limited)
    assert stack.stats("primary").requests == 50  # rejected requests never reached a provider
    assert await stack.limiter.window_tokens(tenant_id(KEY_A)) == 50_000


async def test_admission_sum_never_exceeds_limit_while_requests_are_in_flight(stack) -> None:
    """Sample the window continuously during a burst: reservations in flight never pass 50,000."""
    stack.set("primary", usage_total=700, delay_ms=20)
    peak = 0
    done = asyncio.Event()

    async def sampler() -> None:
        nonlocal peak
        while not done.is_set():
            peak = max(peak, await stack.limiter.window_tokens(tenant_id(KEY_A)))
            await asyncio.sleep(0)

    sampling = asyncio.create_task(sampler())
    sizes = [700 + (i * 137) % 2300 for i in range(90)]  # mixed sizes, ~150,000 requested in total
    responses = await asyncio.gather(*(stack.chat(body_with_tokens(n)) for n in sizes))
    done.set()
    await sampling

    admitted = sum(n for n, r in zip(sizes, responses, strict=True) if r.status_code == 200)
    assert 0 < admitted and peak <= 50_000
    assert {r.status_code for r in responses} <= {200, 429}


async def test_tenants_do_not_share_a_budget(stack) -> None:
    stack.set("primary", usage_total=1000)
    a = await asyncio.gather(*(stack.chat(body_with_tokens(1000), key=KEY_A) for _ in range(60)))
    b = await asyncio.gather(*(stack.chat(body_with_tokens(1000), key=KEY_B) for _ in range(60)))
    assert sum(r.status_code == 200 for r in a) == 50
    assert sum(r.status_code == 200 for r in b) == 50
