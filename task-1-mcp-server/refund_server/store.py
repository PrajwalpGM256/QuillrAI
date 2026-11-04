"""State: read-only customer fixture plus a durable SQLite refund ledger.

The ledger doubles as the idempotency store. A refund and its idempotency key are written in
the same transaction, so a key is either absent (refund never happened) or bound to exactly
one refund. That property holds across process restarts, which is when agent retries happen.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from refund_server import errors

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    status: str
    currency: str
    created_at: datetime
    refundable_total: Decimal


@dataclass(frozen=True)
class RefundRow:
    refund_id: str
    idempotency_key: str
    request_fingerprint: str
    customer_id: str
    amount: Decimal
    currency: str
    reason: str
    created_at: datetime
    remaining_after: Decimal


class CustomerStore:
    def __init__(self, fixture_path: Path) -> None:
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        self._customers = {
            c["customer_id"]: Customer(
                customer_id=c["customer_id"],
                name=c["name"],
                email=c["email"],
                status=c["status"],
                currency=c["currency"],
                created_at=datetime.fromisoformat(c["created_at"]),
                refundable_total=Decimal(c["refundable_total"]),
            )
            for c in raw["customers"]
        }

    def get(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)


def request_fingerprint(customer_id: str, amount: Decimal, reason: str) -> str:
    canonical = json.dumps([customer_id, str(amount.quantize(_CENT)), reason], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS refunds (
    refund_id           TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    customer_id         TEXT NOT NULL,
    amount              TEXT NOT NULL,
    currency            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    remaining_after     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refunds_by_customer ON refunds (customer_id, created_at);
"""


class RefundLedger:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Autocommit mode; transactions are opened explicitly. One connection per operation keeps
        # this safe when the SDK runs sync tool functions on worker threads.
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row(r: sqlite3.Row) -> RefundRow:
        return RefundRow(
            refund_id=r["refund_id"],
            idempotency_key=r["idempotency_key"],
            request_fingerprint=r["request_fingerprint"],
            customer_id=r["customer_id"],
            amount=Decimal(r["amount"]),
            currency=r["currency"],
            reason=r["reason"],
            created_at=datetime.fromisoformat(r["created_at"]),
            remaining_after=Decimal(r["remaining_after"]),
        )

    @staticmethod
    def _refunded_total(conn: sqlite3.Connection, customer_id: str) -> Decimal:
        rows = conn.execute("SELECT amount FROM refunds WHERE customer_id = ?", (customer_id,)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def refunds_for(self, customer_id: str) -> list[RefundRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM refunds WHERE customer_id = ? ORDER BY created_at", (customer_id,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def refundable_balance(self, customer: Customer) -> Decimal:
        with self._connect() as conn:
            return customer.refundable_total - self._refunded_total(conn, customer.customer_id)

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]

    def refund(
        self, customer: Customer, amount: Decimal, reason: str, idempotency_key: str
    ) -> tuple[RefundRow, bool]:
        """Execute a refund at most once per idempotency key.

        Returns ``(row, replayed)``. Raises ``DomainError`` for state-dependent refusals.
        The key lookup, balance check, and insert run under one ``BEGIN IMMEDIATE`` write lock,
        so concurrent calls cannot both pass the balance check or both claim a key.
        """
        amount = amount.quantize(_CENT)
        fingerprint = request_fingerprint(customer.customer_id, amount, reason)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    row = self._row(existing)
                    if row.request_fingerprint != fingerprint:
                        raise errors.DomainError(
                            errors.IDEMPOTENCY_KEY_CONFLICT,
                            "This idempotency_key was already used for a different refund request. "
                            "Use a new key for a new refund, or resend the original arguments to retry.",
                            original_refund_id=row.refund_id,
                        )
                    conn.execute("ROLLBACK")
                    return row, True

                balance = customer.refundable_total - self._refunded_total(conn, customer.customer_id)
                if balance <= 0:
                    raise errors.DomainError(
                        errors.NOTHING_REFUNDABLE,
                        f"Customer {customer.customer_id} has nothing left to refund.",
                        remaining_refundable_balance=float(balance),
                        currency=customer.currency,
                    )
                if amount > balance:
                    raise errors.DomainError(
                        errors.AMOUNT_EXCEEDS_REFUNDABLE,
                        f"Requested {amount} {customer.currency} exceeds the refundable balance of "
                        f"{balance} {customer.currency}.",
                        remaining_refundable_balance=float(balance),
                        currency=customer.currency,
                    )

                row = RefundRow(
                    refund_id=f"re_{uuid.uuid4().hex}",
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    customer_id=customer.customer_id,
                    amount=amount,
                    currency=customer.currency,
                    reason=reason,
                    created_at=datetime.now(UTC),
                    remaining_after=balance - amount,
                )
                conn.execute(
                    "INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.refund_id,
                        row.idempotency_key,
                        row.request_fingerprint,
                        row.customer_id,
                        str(row.amount),
                        row.currency,
                        row.reason,
                        row.created_at.isoformat(),
                        str(row.remaining_after),
                    ),
                )
                conn.execute("COMMIT")
                return row, False
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
