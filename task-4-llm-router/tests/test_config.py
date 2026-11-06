"""Invalid configuration refuses to start."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from llm_router.config import ConfigError, RouterConfig
from tests.procs import ROOT

GOOD = {
    "TENANT_KEYS": "key-one,key-two",
    "PRIMARY_URL": "http://127.0.0.1:9201/v1/chat/completions",
    "PRIMARY_MODEL": "m1",
    "SECONDARY_URL": "http://127.0.0.1:9202/v1/chat/completions",
    "SECONDARY_MODEL": "m2",
}


def test_good_config_and_defaults() -> None:
    c = RouterConfig.from_env(GOOD)
    assert c.tenant_keys == {"key-one", "key-two"}
    assert c.primary.timeout_ms == 3000 and c.secondary.timeout_ms == 3000
    assert c.token_limit == 50_000 and c.window_seconds == 60.0
    assert c.db_path == ".state/router.db"


@pytest.mark.parametrize(
    "override",
    [
        {"TENANT_KEYS": ""},
        {"TENANT_KEYS": "has space"},
        {"PRIMARY_URL": "ftp://x"},
        {"SECONDARY_MODEL": ""},
        {"PRIMARY_TIMEOUT_MS": "0"},
        {"PRIMARY_TIMEOUT_MS": "soon"},
        {"TOKEN_LIMIT": "-5"},
        {"PRIMARY_API_KEY": "key-one"},  # an upstream key must never double as a tenant key
    ],
)
def test_bad_config_raises(override: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        RouterConfig.from_env({**GOOD, **override})


def test_entry_point_exits_2_without_binding() -> None:
    env = {k: v for k, v in os.environ.items() if k not in GOOD}
    proc = subprocess.run(
        [sys.executable, "router.py"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 2 and "refusing to start" in proc.stderr
