"""The gateway's single policy decision point.

Every authorization outcome in the gateway comes from ``decide``. Handlers never inspect tool
names or roles themselves; tools/list filtering asks ``decide`` about each tool as if it were
being called. Anything not explicitly allowed is denied.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum, StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


class DenyKind(Enum):
    UNKNOWN_ROLE = "unknown_role"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    INVALID_PARAMS = "invalid_params"
    UNAUTHORIZED_TOOL = "unauthorized_tool"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    kind: DenyKind | None = None


# Lifecycle methods real clients need to connect (modern discovery and the legacy handshake), plus tools.
ALLOWED_METHODS = frozenset(
    {
        "server/discover",
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
        "tools/call",
    }
)

ADMIN_TOOL_PREFIX = "admin_"


def is_admin_tool(name: str) -> bool:
    """True if ``name`` is an admin tool.

    The name is NFKC-normalized and case-folded before the prefix check, so ADMIN_x, Admin_x and
    full-width variants are all treated as admin tools. This fails closed even if a downstream
    matches tool names more loosely than the spec's case-sensitive SHOULD.
    """
    return unicodedata.normalize("NFKC", name).casefold().startswith(ADMIN_TOOL_PREFIX)


def _allow(reason: str) -> Decision:
    return Decision(allow=True, reason=reason)


def _deny(kind: DenyKind, reason: str) -> Decision:
    return Decision(allow=False, reason=reason, kind=kind)


def decide(role: object, method: object, tool_name: object = None) -> Decision:
    """Decide whether ``role`` may send ``method`` (and, for tools/call, call ``tool_name``).

    ``tool_name`` is whatever the request body carried in ``params.name`` (or None when absent);
    it is typed as ``object`` because validating its shape is part of the decision.
    """
    if not isinstance(role, Role):
        return _deny(DenyKind.UNKNOWN_ROLE, "unknown_role")
    if not isinstance(method, str) or method not in ALLOWED_METHODS:
        return _deny(DenyKind.METHOD_NOT_ALLOWED, "method_not_allowed")
    if method != "tools/call":
        return _allow("method_allowed")
    if not isinstance(tool_name, str) or tool_name == "":
        return _deny(DenyKind.INVALID_PARAMS, "invalid_tool_name")
    if is_admin_tool(tool_name):
        if role is Role.ADMIN:
            return _allow("admin_tool_admin_role")
        return _deny(DenyKind.UNAUTHORIZED_TOOL, "admin_tool_requires_admin")
    return _allow("tool_allowed")
