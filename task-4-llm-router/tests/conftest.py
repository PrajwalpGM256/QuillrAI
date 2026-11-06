from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from hypothesis import HealthCheck, settings

from llm_router.clock import FakeClock
from llm_router.limiter import SqliteWindowLimiter

settings.register_profile(
    "ci", derandomize=True, max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.register_profile(
    "deep", max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

T0 = 1_800_000_000.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


@pytest.fixture
async def stack(tmp_path, clock):
    from tests.helpers import make_config, running

    async with running(make_config(tmp_path / "router.db"), clock=clock) as s:
        yield s


@pytest.fixture
async def limiter(tmp_path, clock) -> AsyncIterator[SqliteWindowLimiter]:
    lim = SqliteWindowLimiter(tmp_path / "limiter.db", limit=50_000, window_seconds=60.0, clock=clock)
    await lim.start()
    try:
        yield lim
    finally:
        await lim.close()
