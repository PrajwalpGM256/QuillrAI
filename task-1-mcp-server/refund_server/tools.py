"""The MCP server and its two tools.

Error channels (see README "Error channels"):
- JSON-RPC protocol error: the tools/call envelope itself is broken. Unknown tool -> -32602;
  a CallToolRequest that fails its schema (e.g. non-object ``arguments``) -> -32602 from the SDK.
- Tool execution error (``isError: true``): everything else. Arguments that fail the input
  schema carry code ``invalid_arguments`` (SEP-1303); state-dependent refusals carry a domain code.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, CallToolResult, InputRequiredResult, ToolAnnotations
from pydantic import ValidationError

from refund_server import errors
from refund_server.models import (
    CustomerId,
    CustomerRecord,
    IdempotencyKey,
    Reason,
    RefundAmount,
    RefundResult,
    RefundSummary,
)
from refund_server.store import Customer, CustomerStore, RefundLedger

logger = logging.getLogger(__name__)

GET_CUSTOMER_RECORD_DESCRIPTION = (
    "Look up a customer by ID (format CUST-12345) and return their profile, account status, current "
    "refundable balance, and past refunds. Read-only: never changes any data, and calling it repeatedly "
    "with the same ID is safe. Use it to check the refundable balance before calling trigger_refund."
)

TRIGGER_REFUND_DESCRIPTION = (
    "Issue a refund to a customer. This moves money and CANNOT be undone. Only call it when the user has "
    "clearly asked for this refund. You must supply an idempotency_key that you generate once per intended "
    "refund. If a call times out, fails ambiguously, or you are unsure whether it went through, retry with "
    "the SAME idempotency_key; never generate a new key for the same refund, or the customer may be "
    "refunded twice. Use a new key only for a genuinely new refund. If the amount exceeds what is "
    "refundable, the error tells you the remaining refundable balance."
)


class RefundMCPServer(MCPServer):
    """MCPServer with two spec-alignment fixes on the tools/call path.

    1. Unknown tool -> JSON-RPC -32602. The SDK reports it as ``isError``; the MCP spec's Tools ->
       Error Handling lists "Unknown tool" as a protocol error.
    2. Argument validation failures keep the SDK's channel (``isError``, per SEP-1303) but are
       reformatted into the stable ``invalid_arguments`` payload, and unexpected arguments are
       rejected instead of silently ignored (the advertised schema says ``additionalProperties: false``).
    Domain errors are unwrapped from the SDK's "Error executing tool ..." prefix so the payload is
    exactly the JSON the tool raised.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {name}")

        unexpected = sorted(set(arguments) - set(tool.parameters.get("properties", {})))
        if unexpected:
            raise errors.invalid_arguments_unexpected(unexpected)

        try:
            return await super().call_tool(name, arguments, context)
        except UnexpectedToolError:
            raise
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                # `from cause` keeps the SDK's field-names-only logging for rejected arguments.
                raise errors.invalid_arguments_from_validation(cause) from cause
            if isinstance(cause, errors.DomainError):
                raise ToolError(str(cause)) from cause
            raise


def build_server(customers_path: Path, db_path: Path) -> RefundMCPServer:
    customers = CustomerStore(customers_path)
    ledger = RefundLedger(db_path)
    server = RefundMCPServer(name="refund-mcp-server", version="0.1.0")

    def _customer_or_error(customer_id: str) -> Customer:
        customer = customers.get(customer_id)
        if customer is None:
            raise errors.DomainError(
                errors.CUSTOMER_NOT_FOUND,
                f"No customer exists with ID {customer_id}.",
                customer_id=customer_id,
            )
        return customer

    @server.tool(
        title="Get customer record",
        description=GET_CUSTOMER_RECORD_DESCRIPTION,
        annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )
    def get_customer_record(customer_id: CustomerId) -> CustomerRecord:
        customer = _customer_or_error(customer_id)
        refunds = ledger.refunds_for(customer_id)
        balance = customer.refundable_total - sum((r.amount for r in refunds), Decimal("0"))
        logger.info("get_customer_record customer_id=%s outcome=ok", customer_id)
        return CustomerRecord(
            customer_id=customer.customer_id,
            name=customer.name,
            email=customer.email,
            status=customer.status,  # type: ignore[arg-type]
            currency=customer.currency,
            refundable_balance=balance,
            created_at=customer.created_at,
            refunds=[
                RefundSummary(
                    refund_id=r.refund_id, amount=r.amount, reason=r.reason, created_at=r.created_at
                )
                for r in refunds
            ],
        )

    @server.tool(
        title="Trigger refund",
        description=TRIGGER_REFUND_DESCRIPTION,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    def trigger_refund(
        customer_id: CustomerId,
        amount: RefundAmount,
        reason: Reason,
        idempotency_key: IdempotencyKey,
    ) -> RefundResult:
        customer = _customer_or_error(customer_id)
        try:
            row, replayed = ledger.refund(customer, amount, reason, idempotency_key)
        except errors.DomainError as exc:
            logger.info("trigger_refund customer_id=%s outcome=%s", customer_id, exc.code)
            raise
        logger.info(
            "trigger_refund customer_id=%s refund_id=%s outcome=%s",
            customer_id,
            row.refund_id,
            "replayed" if replayed else "succeeded",
        )
        return RefundResult(
            refund_id=row.refund_id,
            customer_id=row.customer_id,
            amount=row.amount,
            currency=row.currency,
            status="succeeded",
            idempotency_key=row.idempotency_key,
            replayed=replayed,
            created_at=row.created_at,
            remaining_refundable_balance=row.remaining_after,
        )

    # Advertise what call_tool enforces: no arguments beyond the declared ones.
    for tool in server._tool_manager.list_tools():
        tool.parameters["additionalProperties"] = False

    return server
