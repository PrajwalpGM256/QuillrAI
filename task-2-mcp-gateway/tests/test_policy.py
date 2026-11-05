"""Unit table for the single policy decision point."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_gateway.policy import ALLOWED_METHODS, DenyKind, Role, decide

A, V = Role.ADMIN, Role.VIEWER


@pytest.mark.parametrize(
    ("name", "admin_tool"),
    [
        ("get_weather", False),
        ("administrator_report", False),  # starts with "admin" but not "admin_"
        ("xadmin_y", False),
        ("admin", False),
        ("admin_reset_cache", True),
        ("admin_", True),
        ("ADMIN_reset", True),
        ("Admin_Notes", True),
        ("ａｄｍｉｎ_reset", True),  # full-width letters, NFKC-normalized
        ("ＡＤＭＩＮ＿reset", True),  # full-width letters and full-width low line
    ],
)
def test_admin_tool_classification(name: str, admin_tool: bool) -> None:
    viewer = decide(V, "tools/call", name)
    admin = decide(A, "tools/call", name)
    assert admin.allow is True
    assert viewer.allow is (not admin_tool)
    if admin_tool:
        assert viewer.kind is DenyKind.UNAUTHORIZED_TOOL
        assert viewer.reason == "admin_tool_requires_admin"


@pytest.mark.parametrize("role", [A, V])
@pytest.mark.parametrize("method", sorted(ALLOWED_METHODS - {"tools/call"}))
def test_allowlisted_methods_allowed_for_every_role(role: Role, method: str) -> None:
    assert decide(role, method).allow is True


@pytest.mark.parametrize("role", [A, V])
@pytest.mark.parametrize(
    "method",
    ["resources/read", "resources/list", "prompts/get", "subscriptions/listen", "tools/CALL", "", None, 7],
)
def test_unlisted_methods_denied_for_every_role(role: Role, method: object) -> None:
    decision = decide(role, method, "get_weather")
    assert decision.allow is False and decision.kind is DenyKind.METHOD_NOT_ALLOWED


@pytest.mark.parametrize("role", [A, V])
@pytest.mark.parametrize("name", [None, "", 123, 1.5, True, ["admin_x"], {"name": "x"}])
def test_tools_call_with_bad_name_shape_denied(role: Role, name: object) -> None:
    decision = decide(role, "tools/call", name)
    assert decision.allow is False and decision.kind is DenyKind.INVALID_PARAMS


@pytest.mark.parametrize("role", ["admin", "root", None, 0])
def test_non_role_values_denied(role: object) -> None:
    # Even the string "admin" is not a Role: only resolved Role members pass.
    decision = decide(role, "tools/list")
    assert decision.allow is False and decision.kind is DenyKind.UNKNOWN_ROLE


def test_admin_prefix_literal_lives_only_in_policy() -> None:
    """No scattered prefix checks: the gateway package mentions "admin_" only in policy.py."""
    package = Path(__file__).resolve().parent.parent / "mcp_gateway"
    offenders = [
        p.name for p in package.glob("*.py") if p.name != "policy.py" and "admin_" in p.read_text("utf-8")
    ]
    assert offenders == []
