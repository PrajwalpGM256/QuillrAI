"""SQLite never runs on the event loop.

1. Every SQL statement executed while serving requests runs on the limiter's worker thread.
2. While the DB thread is stalled for 500 ms, the event loop keeps running: a ticker stays on time and
   a request that does not need the database is answered immediately.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tests.helpers import body_with_tokens

pytestmark = pytest.mark.anyio


async def test_every_statement_runs_on_the_worker_thread(stack) -> None:
    threads: list[int] = []
    await stack.limiter.inspect(
        lambda c: c.set_trace_callback(lambda _sql: threads.append(threading.get_ident()))
    )
    stack.set("primary", usage_total=10)
    for _ in range(3):
        assert (await stack.chat(body_with_tokens(20))).status_code == 200
    stack.set("primary", status=500)
    await stack.chat(body_with_tokens(20))  # release path
    await stack.limiter.inspect(lambda c: c.set_trace_callback(None))

    loop_thread = threading.get_ident()
    assert len(threads) >= 20
    assert set(threads) == {stack.limiter.thread_id}
    assert loop_thread not in threads


async def test_loop_stays_responsive_during_a_db_stall(stack) -> None:
    lateness: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            start = time.perf_counter()
            await asyncio.sleep(0.01)
            lateness.append(time.perf_counter() - start - 0.01)

    ticking = asyncio.create_task(ticker())
    stall = asyncio.create_task(stack.limiter.inspect(lambda _c: time.sleep(0.5)))
    await asyncio.sleep(0.05)  # the stall is now holding the DB thread

    started = time.perf_counter()
    r = await stack.chat(key=None)  # 401 path: needs no database
    unauthenticated_latency = time.perf_counter() - started

    await stall
    stop.set()
    await ticking

    assert r.status_code == 401
    assert unauthenticated_latency < 0.1  # answered while the DB thread was still asleep
    assert len(lateness) >= 20 and max(lateness) < 0.05
