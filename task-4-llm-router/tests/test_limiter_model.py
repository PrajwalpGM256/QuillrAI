"""Model-based test: the SQLite limiter against a pure-Python model of the sliding window.

Hypothesis drives random sequences of reserve, settle, release and clock advances across two tenants.
After every step the limiter and the model must agree on each decision, on Retry-After, and on every
tenant's window sum, and no admission may ever push the reserved sum past the limit.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from llm_router.clock import FakeClock
from llm_router.limiter import Rejected, Reservation, SqliteWindowLimiter
from tests.conftest import T0

LIMIT = 50_000
WINDOW = 60.0
TENANTS = st.sampled_from(["a", "b"])


@dataclass
class Row:
    id: int
    tenant: str
    ts: float
    tokens: int


class LimiterMachine(RuleBasedStateMachine):
    reservations = Bundle("reservations")

    def __init__(self) -> None:
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(T0)
        self.runner = asyncio.Runner()
        self.limiter = SqliteWindowLimiter(
            Path(self.tmp.name) / "m.db", limit=LIMIT, window_seconds=WINDOW, clock=self.clock
        )
        self.run(self.limiter.start())
        self.rows: list[Row] = []  # physical rows, including expired ones not yet evicted

    def run(self, coro):
        return self.runner.run(coro)

    def teardown(self) -> None:
        self.run(self.limiter.close())
        self.runner.close()
        self.tmp.cleanup()

    def model_sum(self, tenant: str) -> int:
        horizon = self.clock.now() - WINDOW
        return sum(r.tokens for r in self.rows if r.tenant == tenant and r.ts > horizon)

    @rule(target=reservations, tenant=TENANTS, tokens=st.integers(1, 30_000))
    def reserve(self, tenant: str, tokens: int):
        now = self.clock.now()
        self.rows = [r for r in self.rows if r.ts > now - WINDOW]  # eviction inside reserve
        used = self.model_sum(tenant)
        result = self.run(self.limiter.reserve(tenant, tokens))
        if used + tokens > LIMIT:
            assert isinstance(result, Rejected)
            freed, expected_retry = 0, max(1, math.ceil(WINDOW))
            for r in sorted((r for r in self.rows if r.tenant == tenant), key=lambda r: r.ts):
                freed += r.tokens
                if freed >= used + tokens - LIMIT:
                    expected_retry = max(1, math.ceil(r.ts + WINDOW - now))
                    break
            assert result.retry_after == expected_retry
            return None
        assert isinstance(result, Reservation)
        assert used + tokens <= LIMIT  # admission never exceeds the limit
        self.rows.append(Row(result.id, tenant, now, tokens))
        return result

    @rule(res=consumes(reservations), actual=st.integers(0, 40_000))
    def settle(self, res, actual: int) -> None:
        if res is None:
            return
        self.run(self.limiter.settle(res, actual))
        row = next((r for r in self.rows if r.id == res.id), None)
        if row is not None:
            row.tokens = actual
        elif actual > 0:
            self.rows.append(Row(-1, res.tenant, self.clock.now(), actual))

    @rule(res=consumes(reservations))
    def release(self, res) -> None:
        if res is None:
            return
        self.run(self.limiter.release(res))
        self.rows = [r for r in self.rows if r.id != res.id]

    @rule(seconds=st.floats(0, 90, allow_nan=False))
    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    @invariant()
    def sums_agree(self) -> None:
        for tenant in ("a", "b"):
            assert self.run(self.limiter.window_tokens(tenant)) == self.model_sum(tenant)


TestLimiterModel = LimiterMachine.TestCase
