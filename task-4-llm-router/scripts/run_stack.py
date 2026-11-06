"""Run two mock providers and the router locally for manual testing. Ctrl+C stops all three.

uv run python scripts/run_stack.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIMARY_PORT = os.environ.get("PRIMARY_PORT", "9201")
SECONDARY_PORT = os.environ.get("SECONDARY_PORT", "9202")
ROUTER_PORT = os.environ.get("ROUTER_PORT", "8200")
TENANT_KEYS = "demo-tenant-a,demo-tenant-b"


def main() -> int:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "mock_provider.server"],
            cwd=ROOT,
            env={**env, "PORT": port, "MOCK_NAME": name},
        )
        for name, port in (("primary", PRIMARY_PORT), ("secondary", SECONDARY_PORT))
    ]
    procs.append(
        subprocess.Popen(
            [sys.executable, "router.py"],
            cwd=ROOT,
            env={
                **env,
                "PORT": ROUTER_PORT,
                "TENANT_KEYS": env.get("TENANT_KEYS", TENANT_KEYS),
                "PRIMARY_URL": f"http://127.0.0.1:{PRIMARY_PORT}/v1/chat/completions",
                "PRIMARY_MODEL": "mock-primary",
                "SECONDARY_URL": f"http://127.0.0.1:{SECONDARY_PORT}/v1/chat/completions",
                "SECONDARY_MODEL": "mock-secondary",
            },
        )
    )
    sys.stderr.write(
        f"\nRouter:    http://127.0.0.1:{ROUTER_PORT}/v1/chat/completions\n"
        f"Primary:   http://127.0.0.1:{PRIMARY_PORT}/_mock   Secondary: http://127.0.0.1:{SECONDARY_PORT}/_mock\n"
        f"Tenant keys: {TENANT_KEYS}. Ctrl+C to stop.\n\n"
    )
    try:
        procs[-1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
