"""Audit trail: exactly one structured record per request the gateway handles.

Records go to the ``mcp_gateway.audit`` logger as one JSON object per line. They carry a token
fingerprint, never the token, and never tool arguments.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

audit_logger = logging.getLogger("mcp_gateway.audit")


@dataclass
class AuditRecord:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    token_fp: str | None = None
    role: str | None = None
    method: str | None = None
    tool: str | None = None
    jsonrpc_id: Any = None
    outcome: str = "deny"  # fail closed: a record that never reaches a decision is a denial
    reason: str = "unhandled"
    http_status: int | None = None
    downstream_status: int | None = None

    def emit(self) -> None:
        audit_logger.info(json.dumps(asdict(self), default=str, ensure_ascii=False))
