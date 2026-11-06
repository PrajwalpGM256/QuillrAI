"""Over real sockets: time to first token, and a mid-stream connection drop.

These run the mock provider and the guardrail as uvicorn subprocesses. httpx2's ASGITransport buffers
an entire response before returning it, so an in-process test would report a first-chunk time equal to
the full response time and prove nothing about streaming.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest

from llm_guardrail.sse import SSEParser

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.anyio

EVENTS = 40
DELAY_MS = 25


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited early with {proc.returncode}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"port {port} never opened")


@pytest.fixture(scope="module")
def guardrail_url() -> Iterator[str]:
    provider_port, guardrail_port = _free_port(), _free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    provider = subprocess.Popen(
        [sys.executable, "-m", "mock_provider.server"],
        cwd=ROOT,
        env={**env, "PORT": str(provider_port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    guardrail = subprocess.Popen(
        [sys.executable, "guardrail.py"],
        cwd=ROOT,
        env={
            **env,
            "UPSTREAM_URL": f"http://127.0.0.1:{provider_port}/v1/chat/completions",
            "PORT": str(guardrail_port),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(provider_port, provider)
        _wait_for_port(guardrail_port, guardrail)
        yield f"http://127.0.0.1:{guardrail_port}/v1/chat/completions"
    finally:
        for proc in (guardrail, provider):
            proc.terminate()
            proc.wait(timeout=10)


async def test_first_chunk_arrives_before_the_response_is_buffered(guardrail_url: str) -> None:
    text = "word " * (EVENTS - 1) + "mail a@b.co ssn 123-45-6789 end"
    chunk_size = -(-len(text) // EVENTS)  # ceil: exactly EVENTS content events
    body = {
        "model": "mock-1",
        "stream": True,
        "messages": [],
        "mock": {"text": text, "chunk_size": chunk_size, "delay_ms": DELAY_MS},
    }
    parser = SSEParser()
    first_content: float | None = None
    received = ""
    async with httpx2.AsyncClient(timeout=30) as client:
        start = time.perf_counter()
        async with client.stream("POST", guardrail_url, json=body) as r:
            assert r.status_code == 200
            async for chunk in r.aiter_raw():
                for event in parser.feed(chunk):
                    if event.data in (None, "[DONE]"):
                        continue
                    for choice in json.loads(event.data)["choices"]:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            received += piece
                            if first_content is None:
                                first_content = time.perf_counter() - start
        total = time.perf_counter() - start

    # Reported with -s; the assertions below hold either way.
    sys.stderr.write(f"\nTTFT {first_content * 1000:.0f} ms, full response {total * 1000:.0f} ms")
    sys.stderr.write(f" ({EVENTS} events x {DELAY_MS} ms)\n")
    assert received.endswith("mail [REDACTED] ssn [REDACTED] end")
    assert total >= EVENTS * DELAY_MS / 1000 * 0.9  # the provider really paced the stream
    assert first_content is not None
    assert first_content < 0.3
    assert first_content < total / 3


async def test_real_mid_stream_drop_fails_closed(guardrail_url: str) -> None:
    body = {
        "model": "mock-1",
        "stream": True,
        "messages": [],
        "mock": {"text": "ok 123-45-6789 done", "splits": [12], "break_after": 1},
    }
    async with httpx2.AsyncClient(timeout=30) as client:
        r = await client.post(guardrail_url, json=body)
    data = [e.data for e in SSEParser().feed(r.content) if e.data is not None]
    assert "[DONE]" not in data
    assert json.loads(data[-1])["error"]["code"] == "stream_failed"
    assert "123" not in r.text  # the held '123-45-67' was dropped, not flushed
