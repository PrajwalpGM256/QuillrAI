"""Invalid configuration refuses to start."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from llm_guardrail.config import ConfigError, GuardrailConfig

ROOT = Path(__file__).resolve().parent.parent


def test_good_config() -> None:
    c = GuardrailConfig.from_env({"UPSTREAM_URL": "http://127.0.0.1:9100/v1/chat/completions"})
    assert c.upstream_api_key is None and c.upstream_timeout == 60.0


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"UPSTREAM_URL": "ftp://x"},
        {"UPSTREAM_URL": "http://x", "UPSTREAM_TIMEOUT": "0"},
        {"UPSTREAM_URL": "http://x", "UPSTREAM_TIMEOUT": "soon"},
        {"UPSTREAM_URL": "http://x", "MAX_REQUEST_BYTES": "-1"},
    ],
)
def test_bad_config_raises(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        GuardrailConfig.from_env(env)


def test_entry_point_exits_2_without_binding() -> None:
    env = {k: v for k, v in os.environ.items() if k != "UPSTREAM_URL"}
    proc = subprocess.run(
        [sys.executable, "guardrail.py"], cwd=ROOT, env=env, capture_output=True, timeout=30, text=True
    )
    assert proc.returncode == 2 and "refusing to start" in proc.stderr
