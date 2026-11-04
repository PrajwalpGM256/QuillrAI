"""Error-channel conformance through the SDK client. Assertions are on codes, never on prose.

Channel rule:
- JSON-RPC protocol error  -> the tools/call envelope is broken (unknown tool, malformed CallToolRequest).
- isError tool result      -> everything else; `invalid_arguments` for schema failures (SEP-1303),
                              a domain code for state-dependent refusals.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
from mcp import Client, MCPError
from mcp_types import INVALID_PARAMS

from tests.conftest import error_of, refund_args

pytestmark = pytest.mark.anyio


async def _tools(client: Client) -> dict[str, Any]:
    listing = await client.list_tools()
    return {t.name: t for t in listing.tools}


# --- tools/list -------------------------------------------------------------------------------


async def test_tools_list_advertises_annotations_and_schemas(client: Client) -> None:
    tools = await _tools(client)
    assert set(tools) == {"get_customer_record", "trigger_refund"}

    read = tools["get_customer_record"].annotations
    assert (read.read_only_hint, read.idempotent_hint) == (True, True)

    refund = tools["trigger_refund"].annotations
    assert refund.read_only_hint is False
    assert refund.destructive_hint is True
    assert refund.idempotent_hint is False

    for tool in tools.values():
        assert tool.input_schema["additionalProperties"] is False
        assert tool.output_schema is not None
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        jsonschema.Draft202012Validator.check_schema(tool.output_schema)
    assert tools["trigger_refund"].input_schema["required"] == [
        "customer_id",
        "amount",
        "reason",
        "idempotency_key",
    ]


async def test_trigger_refund_description_states_irreversibility_and_key_reuse(client: Client) -> None:
    desc = (await _tools(client))["trigger_refund"].description
    assert "CANNOT be undone" in desc
    assert "SAME idempotency_key" in desc


# --- protocol errors (JSON-RPC) --------------------------------------------------------------


async def test_unknown_tool_is_jsonrpc_invalid_params(client: Client) -> None:
    with pytest.raises(MCPError) as exc_info:
        await client.call_tool("delete_customer", {"customer_id": "CUST-00001"})
    assert exc_info.value.code == INVALID_PARAMS


# --- invalid_arguments (isError) -------------------------------------------------------------

MALFORMED = [
    ("get_customer_record", {"customer_id": "CUST-1234"}, "customer_id"),
    ("get_customer_record", {"customer_id": "cust-00001"}, "customer_id"),
    ("get_customer_record", {"customer_id": "CUST-ABCDE"}, "customer_id"),
    ("get_customer_record", {}, "customer_id"),
    ("trigger_refund", refund_args(customer_id="12345"), "customer_id"),
    ("trigger_refund", refund_args(amount=0), "amount"),
    ("trigger_refund", refund_args(amount=-5), "amount"),
    ("trigger_refund", refund_args(amount=10.001), "amount"),
    ("trigger_refund", refund_args(amount="25.50"), "amount"),
    ("trigger_refund", refund_args(amount=True), "amount"),
    ("trigger_refund", refund_args(reason="too short"), "reason"),
    ("trigger_refund", refund_args(reason="          x"), "reason"),
    ("trigger_refund", refund_args(idempotency_key="short"), "idempotency_key"),
    ("trigger_refund", {k: v for k, v in refund_args().items() if k != "idempotency_key"}, "idempotency_key"),
    ("trigger_refund", refund_args(currency="USD"), "currency"),
    ("get_customer_record", {"customer_id": "CUST-00001", "verbose": True}, "verbose"),
]


@pytest.mark.parametrize(("tool", "args", "field"), MALFORMED)
async def test_malformed_arguments_are_invalid_arguments_tool_errors(
    client: Client, server, tool: str, args: dict[str, Any], field: str
) -> None:
    result = await client.call_tool(tool, args)
    err = error_of(result)
    assert err["code"] == "invalid_arguments"
    assert field in {d["field"] for d in err["details"]}
    assert result.structured_content is None


async def test_malformed_refund_does_not_touch_the_ledger(client: Client, db_path) -> None:
    await client.call_tool("trigger_refund", refund_args(amount=-1))
    record = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    assert record.structured_content["refunds"] == []


# --- domain errors (isError) -----------------------------------------------------------------


async def test_unknown_customer_is_customer_not_found(client: Client) -> None:
    for tool, args in [
        ("get_customer_record", {"customer_id": "CUST-99999"}),
        ("trigger_refund", refund_args(customer_id="CUST-99999")),
    ]:
        err = error_of(await client.call_tool(tool, args))
        assert err["code"] == "customer_not_found"
        assert err["customer_id"] == "CUST-99999"


async def test_zero_balance_is_nothing_refundable(client: Client) -> None:
    err = error_of(await client.call_tool("trigger_refund", refund_args(customer_id="CUST-00002", amount=1)))
    assert err["code"] == "nothing_refundable"


async def test_overshoot_reports_remaining_balance_and_that_value_succeeds(client: Client) -> None:
    first = await client.call_tool("trigger_refund", refund_args(amount=100, idempotency_key="overshoot-1"))
    assert first.is_error is False

    err = error_of(
        await client.call_tool("trigger_refund", refund_args(amount=500, idempotency_key="overshoot-2"))
    )
    assert err["code"] == "amount_exceeds_refundable"
    record = await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
    assert err["remaining_refundable_balance"] == record.structured_content["refundable_balance"] == 150.0
    assert err["currency"] == "USD"

    # A model that reads the error and retries with the reported value succeeds.
    retry = await client.call_tool(
        "trigger_refund",
        refund_args(amount=err["remaining_refundable_balance"], idempotency_key="overshoot-3"),
    )
    assert retry.is_error is False
    assert retry.structured_content["remaining_refundable_balance"] == 0.0


# --- success: structuredContent conforms to the advertised outputSchema ----------------------


async def test_success_results_conform_to_output_schema(client: Client) -> None:
    tools = await _tools(client)
    calls = [
        ("trigger_refund", refund_args()),
        ("get_customer_record", {"customer_id": "CUST-00001"}),
        ("get_customer_record", {"customer_id": "CUST-00003"}),
    ]
    for name, args in calls:
        result = await client.call_tool(name, args)
        assert result.is_error is False
        # Independent of the SDK: validate against the schema the server advertised.
        jsonschema.validate(result.structured_content, tools[name].output_schema)
        # Backwards-compatible text block mirrors the structured content.
        assert json.loads(result.content[0].text) == result.structured_content

    record = (await client.call_tool("get_customer_record", {"customer_id": "CUST-00001"})).structured_content
    assert record["refundable_balance"] == 250.0 - 25.5
    assert [r["amount"] for r in record["refunds"]] == [25.5]
