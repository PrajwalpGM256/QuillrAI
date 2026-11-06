"""Over real sockets: a slow primary is abandoned, not left running, and the secondary answers."""

from __future__ import annotations

import time

import httpx2

from tests.helpers import KEY_A, body_with_tokens
from tests.procs import configure_mock, free_port, mock_spec, mock_stats, processes, router_spec


def test_slow_primary_is_disconnected_and_secondary_answers(tmp_path) -> None:
    primary, secondary, router = free_port(), free_port(), free_port()
    specs = [
        mock_spec("primary", primary),
        mock_spec("secondary", secondary),
        router_spec(router, primary, secondary, tmp_path / "live.db", PRIMARY_TIMEOUT_MS="300"),
    ]
    with processes(specs):
        configure_mock(primary, delay_ms=5000)
        started = time.perf_counter()
        r = httpx2.post(
            f"http://127.0.0.1:{router}/v1/chat/completions",
            json=body_with_tokens(50),
            headers={"authorization": f"Bearer {KEY_A}"},
            timeout=30,
        )
        elapsed = time.perf_counter() - started
        assert r.status_code == 200 and r.json()["id"] == "chatcmpl-secondary"
        assert elapsed < 2.0  # 300 ms timeout + secondary, not the primary's 5 s

        # The primary provider sees the router hang up: the request is not still running in the background.
        deadline = time.monotonic() + 2.0
        stats = mock_stats(primary)
        while stats["disconnected"] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
            stats = mock_stats(primary)
        assert stats["disconnected"] == 1 and stats["completed"] == 0
