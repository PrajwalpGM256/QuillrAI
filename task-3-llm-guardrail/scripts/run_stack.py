"""Run the mock provider and the guardrail locally for manual testing. Ctrl+C stops both.

uv run python scripts/run_stack.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_PORT = os.environ.get("PROVIDER_PORT", "9100")
GUARDRAIL_PORT = os.environ.get("GUARDRAIL_PORT", "8100")


def main() -> int:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    provider = subprocess.Popen(
        [sys.executable, "-m", "mock_provider.server"], cwd=ROOT, env={**env, "PORT": PROVIDER_PORT}
    )
    guardrail = subprocess.Popen(
        [sys.executable, "guardrail.py"],
        cwd=ROOT,
        env={
            **env,
            "UPSTREAM_URL": f"http://127.0.0.1:{PROVIDER_PORT}/v1/chat/completions",
            "PORT": GUARDRAIL_PORT,
        },
    )
    sys.stderr.write(
        f"\nGuardrail:     http://127.0.0.1:{GUARDRAIL_PORT}/v1/chat/completions\n"
        f"Mock provider: http://127.0.0.1:{PROVIDER_PORT}/v1/chat/completions\n"
        "Ctrl+C to stop.\n\n"
    )
    try:
        guardrail.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (guardrail, provider):
            proc.terminate()
            proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
