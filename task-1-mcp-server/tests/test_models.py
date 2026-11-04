"""Boundary tables for each argument type, exercised directly through Pydantic."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from refund_server.models import CustomerId, IdempotencyKey, Reason, RefundAmount

customer_id = TypeAdapter(CustomerId)
amount = TypeAdapter(RefundAmount)
reason = TypeAdapter(Reason)
key = TypeAdapter(IdempotencyKey)


@pytest.mark.parametrize("value", ["CUST-00000", "CUST-12345", "CUST-99999"])
def test_customer_id_accepts(value: str) -> None:
    assert customer_id.validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "CUST-1234",
        "CUST-123456",
        "cust-12345",
        "CUST-1234A",
        "CUST-ABCDE",
        " CUST-12345",
        "CUST-12345\n",
        "",
        12345,
    ],
)
def test_customer_id_rejects(value: Any) -> None:
    with pytest.raises(ValidationError):
        customer_id.validate_python(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, Decimal("1")),
        (0.01, Decimal("0.01")),
        (10.1, Decimal("10.1")),
        (250, Decimal("250")),
        (99.99, Decimal("99.99")),
    ],
)
def test_amount_accepts(value: Any, expected: Decimal) -> None:
    assert amount.validate_python(value) == expected


@pytest.mark.parametrize(
    "value",
    [0, -1, -0.01, 0.0, 10.001, 0.001, float("inf"), float("nan"), "100", "10.50", True, None, [1]],
)
def test_amount_rejects(value: Any) -> None:
    with pytest.raises(ValidationError):
        amount.validate_python(value)


def test_amount_rejects_json_overflow() -> None:
    # 1e309 parses to +inf in JSON decoders.
    with pytest.raises(ValidationError):
        amount.validate_json("1e309")


@pytest.mark.parametrize("value", ["exactly 10", "Duplicate charge on invoice 1042", "x" * 500])
def test_reason_accepts(value: str) -> None:
    assert reason.validate_python(value) == value


@pytest.mark.parametrize("value", ["123456789", "", " " * 10 + "x", "  short  ", "x" * 501])
def test_reason_rejects(value: str) -> None:
    with pytest.raises(ValidationError):
        reason.validate_python(value)


def test_reason_is_stripped() -> None:
    assert reason.validate_python("   long enough reason   ") == "long enough reason"


@pytest.mark.parametrize(
    "value", ["abcdefgh", "a" * 128, "retry_key-2026-11-03", "6f1c2a9e-8d4b-4f1e-9c3a-1b2c3d4e5f60"]
)
def test_idempotency_key_accepts(value: str) -> None:
    assert key.validate_python(value) == value


@pytest.mark.parametrize("value", ["short", "a" * 129, "has space1", "semi;colon", ""])
def test_idempotency_key_rejects(value: str) -> None:
    with pytest.raises(ValidationError):
        key.validate_python(value)
