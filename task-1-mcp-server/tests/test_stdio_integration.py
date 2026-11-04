"""Drive the real server.py subprocess over stdio with the SDK's own client."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client, MCPError
from mcp_types import INVALID_PARAMS

from tests.conftest import error_of, refund_args, stdio_params

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_session_good_and_bad_input(db_path: Path, mode: str) -> None:
    async with Client(stdio_params(db_path), mode=mode) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"get_customer_record", "trigger_refund"}
        assert tools["trigger_refund"].annotations.destructive_hint is True

        record = await client.call_tool("get_customer_record", {"customer_id": "CUST-00003"})
        assert record.is_error is False
        assert record.structured_content["currency"] == "EUR"

        refund = await client.call_tool("trigger_refund", refund_args(customer_id="CUST-00003", amount=99.99))
        assert refund.is_error is False
        assert refund.structured_content["amount"] == 99.99
        assert refund.structured_content["remaining_refundable_balance"] == 900.01

        assert error_of(await client.call_tool("get_customer_record", {"customer_id": "C-1"}))["code"] == (
            "invalid_arguments"
        )
        assert error_of(await client.call_tool("trigger_refund", refund_args(reason="short")))["code"] == (
            "invalid_arguments"
        )
        assert error_of(await client.call_tool("get_customer_record", {"customer_id": "CUST-54321"}))[
            "code"
        ] == ("customer_not_found")
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("no_such_tool", {})
        assert exc_info.value.code == INVALID_PARAMS
