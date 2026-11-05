"""Interop: the real MCP SDK client, through a real gateway process, to a real downstream process."""

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
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import ADMIN_TOKEN, DOWNSTREAM_TOKEN, TOKENS, VIEWER_TOKEN

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.anyio


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
def gateway_url() -> Iterator[str]:
    down_port, gw_port = _free_port(), _free_port()
    base_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    downstream = subprocess.Popen(
        [sys.executable, "-m", "mock_downstream.server"],
        cwd=ROOT,
        env={**base_env, "DOWNSTREAM_TOKEN": DOWNSTREAM_TOKEN, "PORT": str(down_port), "JSON_RESPONSE": "0"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    gateway = subprocess.Popen(
        [sys.executable, "gateway.py"],
        cwd=ROOT,
        env={
            **base_env,
            "GATEWAY_TOKENS": json.dumps({t: r.value for t, r in TOKENS.items()}),
            "DOWNSTREAM_URL": f"http://127.0.0.1:{down_port}/mcp",
            "DOWNSTREAM_TOKEN": DOWNSTREAM_TOKEN,
            "GATEWAY_PORT": str(gw_port),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(down_port, downstream)
        _wait_for_port(gw_port, gateway)
        yield f"http://127.0.0.1:{gw_port}/mcp"
    finally:
        for proc in (gateway, downstream):
            proc.terminate()
            proc.wait(timeout=10)


def _client(url: str, token: str, mode: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=20)
    return Client(streamable_http_client(url, http_client=http), mode=mode)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sdk_client_through_gateway(gateway_url: str, mode: str) -> None:
    async with _client(gateway_url, VIEWER_TOKEN, mode) as viewer:
        names = {t.name for t in (await viewer.list_tools()).tools}
        assert names == {"get_weather", "administrator_report"}

        result = await viewer.call_tool("get_weather", {"city": "Oslo"})
        assert result.is_error is False
        assert result.content[0].text == "Sunny in Oslo"

        with pytest.raises(MCPError) as denied:
            await viewer.call_tool("admin_reset_cache", {})
        assert denied.value.code == -32001

    async with _client(gateway_url, ADMIN_TOKEN, mode) as admin:
        names = {t.name for t in (await admin.list_tools()).tools}
        assert {"admin_reset_cache", "admin_delete_user"} <= names
        result = await admin.call_tool("admin_reset_cache", {})
        assert result.content[0].text == "cache reset"


async def test_sdk_client_with_bad_token_cannot_connect(gateway_url: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any failure to connect is the expected outcome
        async with _client(gateway_url, "not-a-real-token", "auto") as c:
            await c.list_tools()
