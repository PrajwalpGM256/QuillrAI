"""Validate that a captured stdio stdout stream contains only JSON-RPC 2.0 messages.

Usage:
    python scripts/validate_stdout.py CAPTURED_STDOUT [--sent SENT_STDIN]

Every line of CAPTURED_STDOUT must be one JSON-RPC 2.0 message. The structural check below is
written from the JSON-RPC 2.0 specification, independent of the MCP SDK; each line is then also
parsed with the SDK's own ``JSONRPCMessage`` type as a second opinion. With ``--sent``, response
ids are matched one-to-one against the request ids that were written to the server's stdin.

Exit status 0 when valid, 1 otherwise; findings go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def _is_id(value: Any) -> bool:
    # JSON-RPC 2.0 §4: id is a String, Number, or Null; MCP further forbids null for requests.
    return isinstance(value, str) or (isinstance(value, int | float) and not isinstance(value, bool))


def check_message(msg: Any) -> str | None:
    """Return a description of the first JSON-RPC 2.0 violation in ``msg``, or None if valid."""
    if not isinstance(msg, dict):
        return "message is not a JSON object (batches are not used by MCP)"
    if msg.get("jsonrpc") != "2.0":
        return 'missing or wrong "jsonrpc" member (must be exactly "2.0")'
    if "method" in msg:
        if not isinstance(msg["method"], str):
            return '"method" must be a string'
        if "params" in msg and not isinstance(msg["params"], dict | list):
            return '"params" must be an object or array'
        if "id" in msg and not _is_id(msg["id"]):
            return 'request "id" must be a string or number'
        if "result" in msg or "error" in msg:
            return "request/notification must not carry result or error"
        return None
    # Response.
    if "id" not in msg:
        return 'response is missing "id"'
    has_result, has_error = "result" in msg, "error" in msg
    if has_result == has_error:
        return 'response must contain exactly one of "result" or "error"'
    if has_error:
        err = msg["error"]
        if not isinstance(err, dict):
            return '"error" must be an object'
        if not isinstance(err.get("code"), int) or isinstance(err.get("code"), bool):
            return '"error.code" must be an integer'
        if not isinstance(err.get("message"), str):
            return '"error.message" must be a string'
    elif not _is_id(msg["id"]):
        return 'successful response "id" must be a string or number'
    return None


def validate_lines(
    raw: bytes, sent_request_ids: Iterable[Any] | None = None, *, strict_lf: bool = False
) -> list[str]:
    """Validate a captured stdout byte stream. Returns a list of problems (empty == valid).

    ``strict_lf`` additionally rejects CRLF line endings. It is off by default because the SDK's
    stdio writer uses platform newline translation, so on Windows every message ends in ``\r\n``;
    the ``\r`` is JSON whitespace and parses cleanly, but it is not a bare LF.
    """
    from mcp_types import jsonrpc_message_adapter

    problems: list[str] = []
    if raw and not raw.endswith(b"\n"):
        problems.append("stream does not end with a newline (trailing partial message)")
    response_ids: list[Any] = []
    body = raw[:-1] if raw.endswith(b"\n") else raw
    for lineno, line in enumerate(body.split(b"\n") if body else [], start=1):
        text = line.decode("utf-8", errors="replace")
        if strict_lf and line.endswith(b"\r"):
            problems.append(f"line {lineno}: CRLF line ending (strict_lf)")
        if text.strip() == "":
            problems.append(f"line {lineno}: blank line")
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            problems.append(f"line {lineno}: not JSON ({exc}): {text[:120]!r}")
            continue
        if (why := check_message(msg)) is not None:
            problems.append(f"line {lineno}: {why}")
            continue
        try:
            jsonrpc_message_adapter.validate_python(msg)
        except Exception as exc:  # noqa: BLE001 - any SDK rejection is a finding
            problems.append(f"line {lineno}: rejected by SDK JSONRPCMessage: {exc}")
        if "method" not in msg:
            response_ids.append(msg["id"])

    if sent_request_ids is not None:
        sent = Counter(sent_request_ids)
        got = Counter(response_ids)
        for rid in sorted(set(sent) | set(got), key=repr):
            if got[rid] != sent[rid]:
                problems.append(f"request id {rid!r}: sent {sent[rid]}, got {got[rid]} responses")
    return problems


def _request_ids_from(path: Path) -> list[Any]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            msg = json.loads(line)
            if "method" in msg and "id" in msg:
                ids.append(msg["id"])
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("captured", type=Path)
    parser.add_argument("--sent", type=Path, default=None)
    parser.add_argument("--strict-lf", action="store_true", help="also reject CRLF line endings")
    args = parser.parse_args(argv)
    sent = _request_ids_from(args.sent) if args.sent else None
    problems = validate_lines(args.captured.read_bytes(), sent, strict_lf=args.strict_lf)
    for p in problems:
        sys.stderr.write(p + "\n")
    sys.stderr.write(("OK" if not problems else f"FAIL: {len(problems)} problem(s)") + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
