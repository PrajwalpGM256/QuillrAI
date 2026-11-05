"""Run the mock downstream and the gateway locally for manual testing. Ctrl+C stops both.

    uv run python scripts/run_stack.py

Demo tokens (local only): admin-demo-token (admin), viewer-demo-token (viewer).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOWNSTREAM_PORT = os.environ.get("DOWNSTREAM_PORT", "9000")
GATEWAY_PORT = os.environ.get("GATEWAY_PORT", "8080")
DOWNSTREAM_TOKEN = "demo-gateway-to-downstream"
TOKENS = {"admin-demo-token": "admin", "viewer-demo-token": "viewer"}


def main() -> int:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    downstream = subprocess.Popen(
        [sys.executable, "-m", "mock_downstream.server"],
        cwd=ROOT,
        env={**env, "DOWNSTREAM_TOKEN": DOWNSTREAM_TOKEN, "PORT": DOWNSTREAM_PORT},
    )
    gateway = subprocess.Popen(
        [sys.executable, "gateway.py"],
        cwd=ROOT,
        env={
            **env,
            "GATEWAY_TOKENS": json.dumps(TOKENS),
            "DOWNSTREAM_URL": f"http://127.0.0.1:{DOWNSTREAM_PORT}/mcp",
            "DOWNSTREAM_TOKEN": DOWNSTREAM_TOKEN,
            "GATEWAY_PORT": GATEWAY_PORT,
        },
    )
    sys.stderr.write(
        f"\nGateway: http://127.0.0.1:{GATEWAY_PORT}/mcp  (downstream on :{DOWNSTREAM_PORT})\n"
        "Tokens:  admin-demo-token (admin), viewer-demo-token (viewer)\n"
        "Audit records are the JSON lines on this console. Ctrl+C to stop.\n\n"
    )
    try:
        gateway.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (gateway, downstream):
            proc.terminate()
            proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
