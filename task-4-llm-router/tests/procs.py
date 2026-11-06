"""Start mock providers and router processes as real uvicorn subprocesses."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx2

from tests.helpers import KEY_A, KEY_B, PRIMARY_MODEL, SECONDARY_MODEL

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process exited early with {proc.returncode}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"port {port} never opened")


@contextlib.contextmanager
def processes(specs: list[tuple[list[str], dict[str, str], int]]) -> Iterator[None]:
    procs: list[subprocess.Popen] = []
    try:
        for argv, env, port in specs:
            proc = subprocess.Popen(
                [sys.executable, *argv],
                cwd=ROOT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", **env},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(proc)
            wait_for_port(port, proc)
        yield
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(timeout=10)


def mock_spec(name: str, port: int) -> tuple[list[str], dict[str, str], int]:
    return (["-m", "mock_provider.server"], {"PORT": str(port), "MOCK_NAME": name}, port)


def router_spec(
    port: int, primary: int, secondary: int, db: Path, **env: str
) -> tuple[list[str], dict[str, str], int]:
    return (
        ["router.py"],
        {
            "PORT": str(port),
            "TENANT_KEYS": f"{KEY_A},{KEY_B}",
            "PRIMARY_URL": f"http://127.0.0.1:{primary}/v1/chat/completions",
            "PRIMARY_MODEL": PRIMARY_MODEL,
            "SECONDARY_URL": f"http://127.0.0.1:{secondary}/v1/chat/completions",
            "SECONDARY_MODEL": SECONDARY_MODEL,
            "ROUTER_DB_PATH": str(db),
            **env,
        },
        port,
    )


def configure_mock(port: int, **behaviour) -> None:
    httpx2.post(f"http://127.0.0.1:{port}/_mock", json=behaviour, timeout=5).raise_for_status()


def mock_stats(port: int) -> dict:
    return httpx2.get(f"http://127.0.0.1:{port}/_mock/stats", timeout=5).json()
