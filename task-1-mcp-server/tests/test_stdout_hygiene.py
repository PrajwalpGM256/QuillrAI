"""Prove stdout carries only JSON-RPC: capture every stdout byte of a scripted raw session.

The session is written by hand (no SDK client) so nothing between the server process and the
captured bytes can repair or hide a stray write. It covers success, every error channel, and a
genuine internal fault (the ledger table is dropped mid-session) that makes the SDK log a
traceback, which must land on stderr and nowhere else.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from scripts.validate_stdout import validate_lines
from tests.conftest import PROJECT_ROOT, SERVER_PY, refund_args

TIMEOUT = 15


class RawSession:
    def __init__(self, db_path: Path, stderr_path: Path) -> None:
        self._stderr = stderr_path.open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=PROJECT_ROOT,
            env={**os.environ, "REFUND_DB_PATH": str(db_path), "LOG_LEVEL": "DEBUG"},
        )
        self.captured = bytearray()
        self.sent_lines: list[str] = []
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in iter(self.proc.stdout.readline, b""):
            self.captured += line
            self._lines.put(line)

    def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg)
        self.sent_lines.append(line)
        assert self.proc.stdin is not None
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, rid: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        while True:
            reply = json.loads(self._lines.get(timeout=TIMEOUT))
            if reply.get("id") == rid and "method" not in reply:
                return reply

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=TIMEOUT)
        self._reader.join(timeout=TIMEOUT)
        self._stderr.close()
        return code

    def sent_request_ids(self) -> list[Any]:
        return [m["id"] for m in map(json.loads, self.sent_lines) if "method" in m and "id" in m]


def _call(name: str, arguments: Any) -> dict[str, Any]:
    return {"name": name, "arguments": arguments}


def test_stdout_is_only_jsonrpc_across_scripted_session(tmp_path: Path) -> None:
    db_path = tmp_path / "refunds.db"
    stderr_path = tmp_path / "stderr.log"
    s = RawSession(db_path, stderr_path)
    try:
        init = s.request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "stdout-hygiene-test", "version": "0"},
            },
        )
        assert "result" in init
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        assert len(s.request(2, "tools/list", {})["result"]["tools"]) == 2

        ok_get = s.request(3, "tools/call", _call("get_customer_record", {"customer_id": "CUST-00001"}))
        assert ok_get["result"]["isError"] is False

        ok_refund = s.request("req-four", "tools/call", _call("trigger_refund", refund_args()))
        assert ok_refund["result"]["structuredContent"]["replayed"] is False

        bad_args = s.request(5, "tools/call", _call("trigger_refund", refund_args(amount=-3)))
        assert bad_args["result"]["isError"] is True
        assert json.loads(bad_args["result"]["content"][0]["text"])["error"]["code"] == "invalid_arguments"

        domain = s.request(6, "tools/call", _call("get_customer_record", {"customer_id": "CUST-77777"}))
        assert json.loads(domain["result"]["content"][0]["text"])["error"]["code"] == "customer_not_found"

        unknown = s.request(7, "tools/call", _call("refund_everything", {}))
        assert unknown["error"]["code"] == -32602

        non_object = s.request(8, "tools/call", _call("get_customer_record", "CUST-00001"))
        assert non_object["error"]["code"] == -32602

        # Genuine internal fault: remove the ledger table underneath the running server.
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TABLE refunds")
        fault = s.request(9, "tools/call", _call("get_customer_record", {"customer_id": "CUST-00001"}))
        assert fault["result"]["isError"] is True
        assert "no such table" not in fault["result"]["content"][0]["text"]  # crash detail stays server-side
    finally:
        exit_code = s.close()

    assert exit_code == 0

    captured = bytes(s.captured)
    (tmp_path / "stdout.jsonl").write_bytes(captured)
    # Bare-LF framing is enforced where the SDK emits it; on Windows the SDK writes CRLF (see README).
    assert validate_lines(captured, s.sent_request_ids(), strict_lf=sys.platform != "win32") == []

    # Positive control: logging happened, and it went to stderr, traceback included.
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert "starting stdio server" in stderr
    assert "Traceback" in stderr and "no such table: refunds" in stderr
    assert b"Traceback" not in captured


# --- the validator itself must catch what it claims to catch ---------------------------------

GOOD = b'{"jsonrpc":"2.0","id":1,"result":{}}\n{"jsonrpc":"2.0","method":"notifications/x"}\n'


def test_validator_accepts_clean_stream() -> None:
    assert validate_lines(GOOD, [1]) == []


def test_validator_rejects_stray_print() -> None:
    assert validate_lines(GOOD + b"debug: refund issued\n", [1])


def test_validator_rejects_partial_line() -> None:
    assert validate_lines(GOOD + b'{"jsonrpc":"2.0","id":2', [1])


def test_validator_rejects_non_jsonrpc_json() -> None:
    assert validate_lines(b'{"id":1,"result":{}}\n', [1])
    assert validate_lines(b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1,"message":"x"}}\n', [1])
    assert validate_lines(b'{"jsonrpc":"2.0","id":1,"error":{"code":"-32602","message":"x"}}\n', [1])
    assert validate_lines(b'[{"jsonrpc":"2.0","id":1,"result":{}}]\n', [1])


def test_validator_rejects_missing_or_duplicate_responses() -> None:
    assert validate_lines(GOOD, [1, 2])
    assert validate_lines(GOOD + b'{"jsonrpc":"2.0","id":1,"result":{}}\n', [1])


def test_validator_strict_lf_flags_crlf_only_when_asked() -> None:
    crlf = b'{"jsonrpc":"2.0","id":1,"result":{}}\r\n'
    assert validate_lines(crlf, [1]) == []
    assert validate_lines(crlf, [1], strict_lf=True)


def test_validator_rejects_blank_line() -> None:
    assert validate_lines(GOOD + b"\n", [1])
