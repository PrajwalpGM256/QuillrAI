"""Exercise both tools through the official MCP Inspector (CLI mode) against a throwaway ledger.

Usage:
    uv run python scripts/inspector_check.py

Requires Node (npx). Exits 1 if any check fails.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

INSPECTOR = "@modelcontextprotocol/inspector@2.11.0"
ROOT = Path(__file__).resolve().parent.parent
SERVER_PY = ROOT / "server.py"


def run_inspector(db: Path, *args: str) -> tuple[int, Any]:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("npx not found: install Node.js to run the Inspector check")
    # Inspector does not forward the parent environment, so the ledger path is passed with -e.
    cmd = [npx, "-y", INSPECTOR, "--cli", "-e", f"REFUND_DB_PATH={db}", sys.executable, str(SERVER_PY), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    try:
        return proc.returncode, json.loads(proc.stdout)
    except ValueError:
        return proc.returncode, {"_unparsed_stdout": proc.stdout, "_stderr": proc.stderr[-2000:]}


def call(db: Path, tool: str, **kwargs: Any) -> tuple[int, Any]:
    tool_args = [f"{k}={v}" for k, v in kwargs.items()]
    return run_inspector(db, "--method", "tools/call", "--tool-name", tool, "--tool-arg", *tool_args)


def error_of(result: Any) -> dict[str, Any]:
    try:
        return json.loads(result["content"][0]["text"])["error"]
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def main() -> int:
    failures = 0

    def expect(name: str, ok: bool) -> None:
        nonlocal failures
        sys.stdout.write(f"{'PASS' if ok else 'FAIL'}  {name}\n")
        failures += not ok

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "inspector-check.db"

        code, listing = run_inspector(db, "--method", "tools/list")
        tools = {t["name"]: t for t in listing.get("tools", [])}
        expect(
            "tools/list exposes both tools",
            code == 0 and set(tools) == {"get_customer_record", "trigger_refund"},
        )
        ann = tools.get("trigger_refund", {}).get("annotations", {})
        expect(
            "trigger_refund annotated destructive, not idempotent",
            ann.get("destructiveHint") is True and ann.get("idempotentHint") is False,
        )

        code, r = call(db, "get_customer_record", customer_id="CUST-00001")
        expect(
            "get_customer_record good input -> structuredContent",
            code == 0
            and r.get("isError") is False
            and r.get("structuredContent", {}).get("customer_id") == "CUST-00001",
        )

        _, r = call(db, "get_customer_record", customer_id="CUST-1")
        expect(
            "get_customer_record bad id -> isError invalid_arguments",
            r.get("isError") is True and error_of(r).get("code") == "invalid_arguments",
        )

        _, r = call(db, "get_customer_record", customer_id="CUST-99999")
        expect(
            "get_customer_record unknown customer -> isError customer_not_found",
            r.get("isError") is True and error_of(r).get("code") == "customer_not_found",
        )

        good = {
            "customer_id": "CUST-00001",
            "amount": "12.5",
            "reason": "Inspector scripted refund",
            "idempotency_key": "inspector-0001",
        }
        code, r = call(db, "trigger_refund", **good)
        sc = r.get("structuredContent", {})
        expect(
            "trigger_refund good input -> refund executed",
            code == 0 and sc.get("replayed") is False and sc.get("remaining_refundable_balance") == 237.5,
        )

        code, r = call(db, "trigger_refund", **good)
        sc = r.get("structuredContent", {})
        expect(
            "trigger_refund retry same key -> replayed, not re-executed",
            code == 0 and sc.get("replayed") is True and sc.get("remaining_refundable_balance") == 237.5,
        )

        _, r = call(
            db,
            "trigger_refund",
            customer_id="CUST-00001",
            amount="-1",
            reason="short",
            idempotency_key="inspector-0002",
        )
        expect(
            "trigger_refund bad input -> isError invalid_arguments",
            r.get("isError") is True and error_of(r).get("code") == "invalid_arguments",
        )

        _, r = call(
            db,
            "trigger_refund",
            customer_id="CUST-00001",
            amount="9999",
            reason="Inspector overshoot test",
            idempotency_key="inspector-0003",
        )
        err = error_of(r)
        expect(
            "trigger_refund overshoot -> amount_exceeds_refundable with remaining balance",
            err.get("code") == "amount_exceeds_refundable"
            and err.get("remaining_refundable_balance") == 237.5,
        )

    sys.stdout.write(f"{failures} check(s) failed\n" if failures else "All Inspector checks passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
