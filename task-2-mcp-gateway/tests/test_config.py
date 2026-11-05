"""Invalid configuration refuses to start: the gateway never runs in an undefined auth state."""

from __future__ import annotations

import json

import pytest

from mcp_gateway.config import ConfigError, GatewayConfig

GOOD = {
    "GATEWAY_TOKENS": json.dumps({"admin-tok": "admin", "viewer-tok": "viewer"}),
    "DOWNSTREAM_URL": "http://127.0.0.1:9000/mcp",
    "DOWNSTREAM_TOKEN": "down-tok",
}


def test_good_config_loads() -> None:
    cfg = GatewayConfig.from_env(GOOD)
    assert set(cfg.tokens.values()) == {"admin", "viewer"}
    assert cfg.allowed_origins == frozenset()


@pytest.mark.parametrize(
    "override",
    [
        {"GATEWAY_TOKENS": ""},
        {"GATEWAY_TOKENS": "{}"},
        {"GATEWAY_TOKENS": "[]"},
        {"GATEWAY_TOKENS": json.dumps({"t": "superuser"})},
        {"GATEWAY_TOKENS": json.dumps({"has space": "admin"})},
        {"GATEWAY_TOKENS": json.dumps({"": "viewer"})},
        {"DOWNSTREAM_URL": ""},
        {"DOWNSTREAM_URL": "file:///etc/passwd"},
        {"DOWNSTREAM_TOKEN": ""},
        {"DOWNSTREAM_TOKEN": "admin-tok"},  # a client token may not double as the downstream credential
    ],
)
def test_bad_config_refuses_to_start(override: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({**GOOD, **override})
