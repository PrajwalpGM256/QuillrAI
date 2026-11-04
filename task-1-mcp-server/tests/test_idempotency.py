"""idempotency_key is the agent-retry contract: one logical refund executes at most once."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from refund_server.store import RefundLedger
from tests.conftest import error_of, refund_args, stdio_params

pytestmark = pytest.mark.anyio


async def _balance(client: Client, customer_id: str = "CUST-00001") -> float:
    record = await client.call_tool("get_customer_record", {"customer_id": customer_id})
    return record.structured_content["refundable_balance"]


async def test_agent_timeout_then_retry_same_key_refunds_once(client: Client, db_path: Path) -> None:
    args = refund_args(amount=40, idempotency_key="agent-retry-0001")
    before = await _balance(client)

    # The agent sends the refund, then times out: it never sees this result.
    _lost = await client.call_tool("trigger_refund", args)

    # Not knowing whether it went through, the agent retries with the same key.
    retry = await client.call_tool("trigger_refund", args)

    assert retry.is_error is False
    assert retry.structured_content["replayed"] is True
    assert retry.structured_content["refund_id"] == _lost.structured_content["refund_id"]
    assert RefundLedger(db_path).count() == 1
    assert await _balance(client) == before - 40


async def test_same_key_different_request_is_conflict(client: Client, db_path: Path) -> None:
    first = await client.call_tool("trigger_refund", refund_args(amount=10, idempotency_key="conflict-0001"))
    assert first.is_error is False

    err = error_of(
        await client.call_tool("trigger_refund", refund_args(amount=11, idempotency_key="conflict-0001"))
    )
    assert err["code"] == "idempotency_key_conflict"
    assert err["original_refund_id"] == first.structured_content["refund_id"]
    assert RefundLedger(db_path).count() == 1


async def test_distinct_keys_are_distinct_refunds(client: Client, db_path: Path) -> None:
    for key in ("distinct-0001", "distinct-0002"):
        result = await client.call_tool("trigger_refund", refund_args(amount=10, idempotency_key=key))
        assert result.structured_content["replayed"] is False
    assert RefundLedger(db_path).count() == 2
    assert await _balance(client) == 230.0


async def test_replay_after_balance_exhausted_still_returns_original(client: Client) -> None:
    args = refund_args(amount=250, idempotency_key="exhaust-0001")
    first = await client.call_tool("trigger_refund", args)
    assert first.structured_content["remaining_refundable_balance"] == 0.0
    # The key lookup precedes the balance check, so a retry is a replay, not nothing_refundable.
    retry = await client.call_tool("trigger_refund", args)
    assert retry.is_error is False and retry.structured_content["replayed"] is True


async def test_key_survives_server_restart(db_path: Path) -> None:
    args = refund_args(amount=15, idempotency_key="restart-0001")
    async with Client(stdio_params(db_path)) as first_process:
        original = await first_process.call_tool("trigger_refund", args)
        assert original.structured_content["replayed"] is False

    async with Client(stdio_params(db_path)) as second_process:
        retry = await second_process.call_tool("trigger_refund", args)
        assert retry.structured_content["replayed"] is True
        assert retry.structured_content["refund_id"] == original.structured_content["refund_id"]
        assert await _balance(second_process) == 235.0
