"""The SQLite sliding-window limiter: boundary, eviction, transactions, two-phase accounting."""

from __future__ import annotations

import sqlite3

import pytest

from llm_router.limiter import Rejected, Reservation, SqliteWindowLimiter
from tests.conftest import T0

pytestmark = pytest.mark.anyio


async def row_count(limiter: SqliteWindowLimiter) -> int:
    return await limiter.inspect(lambda c: c.execute("SELECT COUNT(*) FROM usage").fetchone()[0])


async def fill(limiter: SqliteWindowLimiter, tenant: str = "t", n: int = 50, tokens: int = 1000) -> None:
    for _ in range(n):
        assert isinstance(await limiter.reserve(tenant, tokens), Reservation)


# --- window boundary: eviction happens inside the admission transaction ------------------------


async def test_window_boundary_without_any_eviction_call(limiter, clock) -> None:
    await fill(limiter)  # 50 x 1000 = 50,000 at T0
    assert await limiter.window_tokens("t") == 50_000
    assert isinstance(await limiter.reserve("t", 1), Rejected)

    clock.set(T0 + 59.999)  # still inside the window
    assert isinstance(await limiter.reserve("t", 1), Rejected)

    clock.set(T0 + 60.0)  # rows at T0 now satisfy ts <= now - 60: expired
    admitted = await limiter.reserve("t", 1000)
    assert isinstance(admitted, Reservation)
    # The 50 expired rows were deleted by that same reserve() call: no timer, no evict() method.
    assert await row_count(limiter) == 1
    assert await limiter.window_tokens("t") == 1000
    assert not any(name.startswith("evict") for name in dir(limiter))


async def test_sliding_not_fixed_window(limiter, clock) -> None:
    await fill(limiter, n=25)  # 25,000 at T0
    clock.set(T0 + 30)
    await fill(limiter, n=25)  # 25,000 at T0+30: full
    assert isinstance(await limiter.reserve("t", 1), Rejected)
    clock.set(T0 + 60)  # first half expires, second half still counts
    assert isinstance(await limiter.reserve("t", 25_000), Reservation)
    assert isinstance(await limiter.reserve("t", 1), Rejected)


async def test_tenants_are_independent(limiter) -> None:
    await fill(limiter, tenant="a")
    assert isinstance(await limiter.reserve("a", 1), Rejected)
    assert isinstance(await limiter.reserve("b", 50_000), Reservation)


async def test_retry_after_points_at_the_oldest_rows_that_must_expire(limiter, clock) -> None:
    await fill(limiter, n=10, tokens=5000)  # 50,000 at T0
    clock.set(T0 + 45.2)
    rejected = await limiter.reserve("t", 5000)
    assert isinstance(rejected, Rejected)
    assert rejected.retry_after == 15  # ceil(T0 + 60 - (T0 + 45.2))
    assert rejected.window_tokens == 50_000


async def test_over_budget_single_request_is_refused_up_front(limiter) -> None:
    with pytest.raises(ValueError):
        await limiter.reserve("t", 50_001)
    assert isinstance(await limiter.reserve("t", 50_000), Reservation)  # exactly the limit fits


# --- two-phase accounting ----------------------------------------------------------------------


async def test_settle_replaces_reservation_with_actual(limiter) -> None:
    res = await limiter.reserve("t", 1000)
    assert await limiter.window_tokens("t") == 1000
    await limiter.settle(res, 1450)  # output tokens arrive: the window can now exceed the estimate
    assert await limiter.window_tokens("t") == 1450
    state, ts = await limiter.inspect(lambda c: c.execute("SELECT state, ts FROM usage").fetchone())
    assert state == "charged" and ts == res.ts  # keeps the admission timestamp


async def test_release_removes_the_reservation(limiter) -> None:
    res = await limiter.reserve("t", 1000)
    await limiter.release(res)
    assert await limiter.window_tokens("t") == 0 and await row_count(limiter) == 0


async def test_settlement_can_exceed_the_limit_documented_gap(limiter) -> None:
    """Reservations never exceed 50,000 at admission; the settled window can (unpredicted output)."""
    reservations = [await limiter.reserve("t", 1000) for _ in range(50)]
    assert await limiter.window_tokens("t") == 50_000
    for res in reservations:
        await limiter.settle(res, 1200)
    assert await limiter.window_tokens("t") == 60_000
    assert isinstance(await limiter.reserve("t", 1), Rejected)


async def test_settle_after_the_row_expired_still_charges(limiter, clock) -> None:
    res = await limiter.reserve("t", 1000)
    clock.set(T0 + 61)
    other = await limiter.reserve("other", 1)  # evicts the expired reservation
    assert other.id != res.id  # AUTOINCREMENT: an evicted id is never handed out again
    await limiter.settle(res, 700)
    assert await limiter.window_tokens("t") == 700
    assert await limiter.window_tokens("other") == 1  # the stale settle did not touch another row
    await limiter.release(res)
    assert await limiter.window_tokens("other") == 1


# --- SQLite configuration and transactions -----------------------------------------------------


async def test_wal_full_sync_and_busy_timeout(limiter) -> None:
    mode, sync, busy = await limiter.inspect(
        lambda c: (
            c.execute("PRAGMA journal_mode").fetchone()[0],
            c.execute("PRAGMA synchronous").fetchone()[0],
            c.execute("PRAGMA busy_timeout").fetchone()[0],
        )
    )
    assert (mode, sync, busy) == ("wal", 2, 5000)  # synchronous 2 == FULL


async def test_wal_persists_in_the_file(limiter) -> None:
    conn = sqlite3.connect(limiter.path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


async def test_every_write_is_one_begin_immediate_transaction(limiter) -> None:
    statements: list[str] = []
    await limiter.inspect(lambda c: c.set_trace_callback(statements.append))
    res = await limiter.reserve("t", 10)
    await limiter.settle(res, 12)
    await limiter.release(await limiter.reserve("t", 10))
    await limiter.inspect(lambda c: c.set_trace_callback(None))

    sql = [s.split()[0].upper() for s in statements]
    assert sql.count("BEGIN") == 4 and sql.count("COMMIT") == 4
    assert all(s.startswith("BEGIN IMMEDIATE") for s in statements if s.upper().startswith("BEGIN"))
    reserve = statements[: statements.index("COMMIT") + 1]
    assert [s.split()[0].upper() for s in reserve] == ["BEGIN", "DELETE", "SELECT", "INSERT", "COMMIT"]


async def test_failed_transaction_rolls_back(limiter) -> None:
    def broken(conn, _now):
        conn.execute("INSERT INTO usage (tenant, ts, tokens, state) VALUES ('t', 0, 5, 'reserved')")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await limiter._run(limiter._write, broken)  # noqa: SLF001
    assert await row_count(limiter) == 0


async def test_state_survives_restart(tmp_path, clock) -> None:
    path = tmp_path / "persist.db"
    first = SqliteWindowLimiter(path, clock=clock)
    await first.start()
    await first.reserve("t", 49_000)
    await first.close()

    second = SqliteWindowLimiter(path, clock=clock)
    await second.start()
    try:
        assert await second.window_tokens("t") == 49_000
        assert isinstance(await second.reserve("t", 2000), Rejected)
    finally:
        await second.close()
