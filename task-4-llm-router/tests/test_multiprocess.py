"""Two router processes sharing one SQLite file hold one combined token budget.

WAL mode plus BEGIN IMMEDIATE serialize admissions across processes: 80 concurrent requests of 1,000
tokens, split across both routers, admit exactly 50 in total.
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx2
import pytest

from llm_router.app import tenant_id
from tests.helpers import KEY_A, body_with_tokens
from tests.procs import configure_mock, free_port, mock_spec, processes, router_spec

pytestmark = pytest.mark.anyio


async def test_two_processes_share_one_budget(tmp_path) -> None:
    db = tmp_path / "shared.db"
    primary, secondary, router_a, router_b = (free_port() for _ in range(4))
    specs = [
        mock_spec("primary", primary),
        mock_spec("secondary", secondary),
        router_spec(router_a, primary, secondary, db),
        router_spec(router_b, primary, secondary, db),
    ]
    with processes(specs):
        configure_mock(primary, usage_total=1000, delay_ms=50)
        headers = {"authorization": f"Bearer {KEY_A}"}
        async with httpx2.AsyncClient(timeout=30, limits=httpx2.Limits(max_connections=200)) as client:
            urls = [f"http://127.0.0.1:{port}/v1/chat/completions" for port in (router_a, router_b)]
            responses = await asyncio.gather(
                *(client.post(urls[i % 2], json=body_with_tokens(1000), headers=headers) for i in range(80))
            )

        statuses = [r.status_code for r in responses]
        assert statuses.count(200) == 50 and statuses.count(429) == 30
        per_router = [
            sum(1 for i, r in enumerate(responses) if i % 2 == k and r.status_code == 200) for k in (0, 1)
        ]
        assert all(n > 0 for n in per_router), per_router  # both processes admitted some

        conn = sqlite3.connect(db)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            total = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ?", (tenant_id(KEY_A),)
            ).fetchone()[0]
        finally:
            conn.close()
        assert total == 50_000
