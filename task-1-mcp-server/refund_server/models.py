"""Boundary types for tool arguments and tool results.

Tool arguments are declared as reusable ``Annotated`` types so the SDK derives each tool's
``inputSchema`` from them and validates incoming arguments through its own Pydantic path.
Handlers therefore only ever see values that already satisfy these constraints.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    WithJsonSchema,
)

CUSTOMER_ID_PATTERN = r"^CUST-\d{5}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 500


def _require_json_number(value: Any) -> Any:
    """Accept only JSON numbers. Strings such as "100" and booleans are rejected, not coerced."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("amount must be a JSON number")
    return value


# Money is serialized as a JSON number on the wire but held as Decimal internally.
_MoneyOut = PlainSerializer(lambda v: float(v), return_type=float, when_used="json")

CustomerId = Annotated[
    str,
    StringConstraints(pattern=CUSTOMER_ID_PATTERN),
    Field(description="Customer identifier: 'CUST-' followed by exactly five digits, e.g. CUST-00001."),
]

RefundAmount = Annotated[
    Decimal,
    BeforeValidator(_require_json_number),
    Field(gt=0, decimal_places=2, allow_inf_nan=False),
    WithJsonSchema(
        {
            "type": "number",
            "exclusiveMinimum": 0,
            "description": "Refund amount in the customer's currency. Positive, at most 2 decimal places.",
        }
    ),
]

Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=REASON_MIN_LENGTH, max_length=REASON_MAX_LENGTH),
    Field(
        description=(
            f"Why the refund is being issued, {REASON_MIN_LENGTH}-{REASON_MAX_LENGTH} characters "
            "(leading/trailing whitespace ignored)."
        )
    ),
]

IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=IDEMPOTENCY_KEY_PATTERN),
    Field(
        description=(
            "Caller-generated key identifying this one intended refund "
            "(8-128 chars: letters, digits, '_', '-'). "
            "Reuse the same key when retrying the same refund; use a new key only for a new refund."
        )
    ),
]

Money = Annotated[Decimal, _MoneyOut, WithJsonSchema({"type": "number"})]


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class RefundSummary(_Output):
    refund_id: str
    amount: Money
    reason: str
    created_at: datetime


class CustomerRecord(_Output):
    customer_id: str
    name: str
    email: str
    status: Literal["active", "suspended", "closed"]
    currency: str
    refundable_balance: Money = Field(description="Amount that can still be refunded.")
    created_at: datetime
    refunds: list[RefundSummary]


class RefundResult(_Output):
    refund_id: str
    customer_id: str
    amount: Money
    currency: str
    status: Literal["succeeded"]
    idempotency_key: str
    replayed: bool = Field(
        description="True when this result is the stored outcome of an earlier call with the same key."
    )
    created_at: datetime
    remaining_refundable_balance: Money
