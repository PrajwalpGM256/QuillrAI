"""Token-aware sliding-window rate limiter on SQLite.

Off the event loop:
    Every SQLite call runs on one dedicated worker thread that owns the connection. sqlite3 is a
    blocking C library: a call made on the event loop would stop the loop for its whole duration,
    including any wait for another process's write lock (busy_timeout), so every concurrent request
    would queue behind that one database call. On the worker thread only the caller awaits.

Durability and concurrency:
    PRAGMA journal_mode=WAL lets readers proceed while one writer commits, and two router processes
    can share the file. synchronous=FULL fsyncs each commit, so a committed reservation survives power
    loss. Every write runs in BEGIN IMMEDIATE, which takes the write lock at the start of the
    transaction: the window sum and the insert that depends on it cannot interleave with another
    writer, in this process or another.

Eviction:
    Expired rows are deleted inside the same transaction as the window sum and the insert. There is
    no background timer, so the sum is correct at the instant of every decision.

Two-phase accounting:
    reserve() inserts the input-token estimate. settle() replaces it with the provider's actual total.
    release() deletes it on any failure. Output tokens are unknown at admission, so the window sum at
    admission includes reservations, not final charges: after settlement the window can exceed the
    limit by the output tokens that were not predicted.
"""

from __future__ import annotations

import asyncio
import functools
import math
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from llm_router.clock import Clock, SystemClock

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: a stale id cannot hit a newer row
    tenant TEXT    NOT NULL,
    ts     REAL    NOT NULL,
    tokens INTEGER NOT NULL,
    state  TEXT    NOT NULL CHECK (state IN ('reserved', 'charged'))
);
CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage (tenant, ts);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
"""


class LimiterUnavailable(Exception):
    """The database could not be used; the caller must not admit the request."""


@dataclass(frozen=True)
class Reservation:
    id: int
    tenant: str
    tokens: int
    ts: float


@dataclass(frozen=True)
class Rejected:
    retry_after: int  # whole seconds until enough of the window expires
    window_tokens: int


class SqliteWindowLimiter:
    def __init__(
        self,
        path: str | Path,
        *,
        limit: int = 50_000,
        window_seconds: float = 60.0,
        clock: Clock | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = str(path)
        self.limit = limit
        self.window = window_seconds
        self.clock = clock or SystemClock()
        self.busy_timeout_ms = busy_timeout_ms
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._conn: sqlite3.Connection | None = None
        self.thread_id: int | None = None

    # --- worker-thread plumbing ----------------------------------------------------------------

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._executor, functools.partial(fn, *args))
        except sqlite3.Error as exc:
            raise LimiterUnavailable(str(exc)) from exc

    async def inspect(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(connection)`` on the worker thread. For startup checks and tests."""
        return await self._run(lambda: fn(self._connection()))

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("limiter not started")
        return self._conn

    def _open(self) -> None:
        if Path(self.path).parent and self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.close()
            raise sqlite3.OperationalError(f"WAL mode unavailable (got {mode})")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        conn.executescript(SCHEMA)
        self._conn = conn
        self.thread_id = threading.get_ident()

    async def start(self) -> None:
        await self._run(self._open)

    async def close(self) -> None:
        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        try:
            await self._run(_close)
        finally:
            self._executor.shutdown(wait=True)

    def _write(self, body: Callable[[sqlite3.Connection, float], T]) -> T:
        """Run ``body`` inside BEGIN IMMEDIATE ... COMMIT, rolling back on any error."""
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = body(conn, self.clock.now())  # time is read after the write lock is held
            conn.execute("COMMIT")
            return result
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    # --- reserve / settle / release ------------------------------------------------------------

    def _reserve(self, tenant: str, tokens: int) -> Reservation | Rejected:
        def body(conn: sqlite3.Connection, now: float) -> Reservation | Rejected:
            horizon = now - self.window
            conn.execute("DELETE FROM usage WHERE ts <= ?", (horizon,))
            (used,) = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ? AND ts > ?", (tenant, horizon)
            ).fetchone()
            if used + tokens > self.limit:
                return Rejected(self._retry_after(conn, tenant, now, used + tokens - self.limit), used)
            cur = conn.execute(
                "INSERT INTO usage (tenant, ts, tokens, state) VALUES (?, ?, ?, 'reserved')",
                (tenant, now, tokens),
            )
            return Reservation(id=int(cur.lastrowid), tenant=tenant, tokens=tokens, ts=now)

        return self._write(body)

    def _retry_after(self, conn: sqlite3.Connection, tenant: str, now: float, excess: int) -> int:
        """Seconds until the oldest rows holding at least ``excess`` tokens leave the window."""
        freed = 0
        rows = conn.execute(
            "SELECT ts, tokens FROM usage WHERE tenant = ? AND ts > ? ORDER BY ts",
            (tenant, now - self.window),
        )
        for ts, row_tokens in rows:
            freed += row_tokens
            if freed >= excess:
                return max(1, math.ceil(ts + self.window - now))
        return max(1, math.ceil(self.window))

    def _settle(self, reservation: Reservation, actual: int) -> None:
        def body(conn: sqlite3.Connection, now: float) -> None:
            cur = conn.execute(
                "UPDATE usage SET tokens = ?, state = 'charged' WHERE id = ? AND tenant = ?",
                (actual, reservation.id, reservation.tenant),
            )
            if cur.rowcount == 0 and actual > 0:
                # The reservation already left the window: still record the charge, at settlement time.
                conn.execute(
                    "INSERT INTO usage (tenant, ts, tokens, state) VALUES (?, ?, ?, 'charged')",
                    (reservation.tenant, now, actual),
                )

        self._write(body)

    def _release(self, reservation: Reservation) -> None:
        self._write(
            lambda conn, _now: conn.execute(
                "DELETE FROM usage WHERE id = ? AND tenant = ?", (reservation.id, reservation.tenant)
            )
        )

    async def reserve(self, tenant: str, tokens: int) -> Reservation | Rejected:
        if tokens > self.limit:
            raise ValueError("a single request can never fit in the window")
        return await self._run(self._reserve, tenant, tokens)

    async def settle(self, reservation: Reservation, actual: int) -> None:
        # shield: cancelling the request must not drop the charge
        await asyncio.shield(self._run(self._settle, reservation, max(0, int(actual))))

    async def release(self, reservation: Reservation) -> None:
        await asyncio.shield(self._run(self._release, reservation))

    async def window_tokens(self, tenant: str) -> int:
        """Read-only window sum (no eviction): for tests and diagnostics."""

        def read(conn: sqlite3.Connection) -> int:
            horizon = self.clock.now() - self.window
            return conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ? AND ts > ?", (tenant, horizon)
            ).fetchone()[0]

        return await self.inspect(read)
