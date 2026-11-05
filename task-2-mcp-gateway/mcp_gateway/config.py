"""Gateway configuration from the environment. Invalid configuration refuses to start (fail closed)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from mcp_gateway.policy import Role

# RFC 6750 b64token: the only characters a Bearer token may contain.
B64TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GatewayConfig:
    tokens: Mapping[str, Role]
    downstream_url: str
    downstream_token: str
    allowed_origins: frozenset[str] = field(default_factory=frozenset)
    max_body_bytes: int = 1_048_576
    downstream_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ConfigError("GATEWAY_TOKENS must define at least one token")
        for token, role in self.tokens.items():
            if not isinstance(token, str) or not B64TOKEN.fullmatch(token):
                raise ConfigError("every gateway token must be a non-empty RFC 6750 b64token")
            if not isinstance(role, Role):
                raise ConfigError(f"unknown role {role!r}; expected one of {[r.value for r in Role]}")
        if not self.downstream_url.startswith(("http://", "https://")):
            raise ConfigError("DOWNSTREAM_URL must be an http(s) URL")
        if not self.downstream_token or not B64TOKEN.fullmatch(self.downstream_token):
            raise ConfigError("DOWNSTREAM_TOKEN must be a non-empty RFC 6750 b64token")
        if self.downstream_token in self.tokens:
            # A client token must never double as the gateway's downstream credential.
            raise ConfigError("DOWNSTREAM_TOKEN must differ from every client token")
        if self.max_body_bytes <= 0 or self.downstream_timeout <= 0:
            raise ConfigError("limits must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        env = os.environ if env is None else env
        try:
            raw = json.loads(env.get("GATEWAY_TOKENS", ""))
        except ValueError as exc:
            raise ConfigError("GATEWAY_TOKENS must be a JSON object of {token: role}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("GATEWAY_TOKENS must be a JSON object of {token: role}")
        try:
            tokens = {token: Role(role) for token, role in raw.items()}
        except ValueError as exc:
            raise ConfigError(f"GATEWAY_TOKENS has an unknown role: {exc}") from exc
        origins = frozenset(o.strip() for o in env.get("GATEWAY_ALLOWED_ORIGINS", "").split(",") if o.strip())
        return cls(
            tokens=tokens,
            downstream_url=env.get("DOWNSTREAM_URL", ""),
            downstream_token=env.get("DOWNSTREAM_TOKEN", ""),
            allowed_origins=origins,
            max_body_bytes=int(env.get("MAX_BODY_BYTES", "1048576")),
            downstream_timeout=float(env.get("DOWNSTREAM_TIMEOUT", "30")),
        )
