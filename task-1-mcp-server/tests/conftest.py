from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp_types import CallToolResult

from refund_server.tools import RefundMCPServer, build_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = PROJECT_ROOT / "server.py"
CUSTOMERS = PROJECT_ROOT / "data" / "customers.json"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "refunds.db"


@pytest.fixture
def server(db_path: Path) -> RefundMCPServer:
    return build_server(CUSTOMERS, db_path)


@pytest.fixture
async def client(server: RefundMCPServer) -> AsyncIterator[Client]:
    async with Client(server) as c:
        yield c


def stdio_params(db_path: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PY)],
        env={**os.environ, "REFUND_DB_PATH": str(db_path), "LOG_LEVEL": "INFO"},
        cwd=PROJECT_ROOT,
    )


def error_of(result: CallToolResult) -> dict[str, Any]:
    """Parse the machine-readable error payload of an isError tool result."""
    assert result.is_error is True, result
    assert len(result.content) == 1 and result.content[0].type == "text", result.content
    return json.loads(result.content[0].text)["error"]


def refund_args(**overrides: Any) -> dict[str, Any]:
    args = {
        "customer_id": "CUST-00001",
        "amount": 25.5,
        "reason": "Duplicate charge on invoice 1042",
        "idempotency_key": "test-key-0001",
    }
    args.update(overrides)
    return args
