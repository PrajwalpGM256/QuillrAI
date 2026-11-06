"""Router configuration from the environment. Invalid configuration refuses to start."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# RFC 6750 b64token: the characters a Bearer token may contain
B64TOKEN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RouteConfig:
    name: str  # "primary" | "secondary"; used in logs only, never in responses
    url: str
    model: str
    api_key: str | None = None
    timeout_ms: int = 3000

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"{self.name.upper()}_URL must be an http(s) URL")
        if not self.model:
            raise ConfigError(f"{self.name.upper()}_MODEL is required")
        if self.timeout_ms <= 0:
            raise ConfigError(f"{self.name.upper()}_TIMEOUT_MS must be positive")


@dataclass(frozen=True)
class RouterConfig:
    tenant_keys: frozenset[str]
    primary: RouteConfig
    secondary: RouteConfig
    db_path: str = ".state/router.db"
    token_limit: int = 50_000
    window_seconds: float = 60.0
    max_request_bytes: int = 1_048_576
    db_busy_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        if not self.tenant_keys:
            raise ConfigError("TENANT_KEYS must list at least one key")
        if any(not B64TOKEN.fullmatch(k) for k in self.tenant_keys):
            raise ConfigError("every tenant key must be a non-empty RFC 6750 b64token")
        upstream_keys = {self.primary.api_key, self.secondary.api_key} - {None}
        if upstream_keys & self.tenant_keys:
            raise ConfigError("upstream API keys must differ from every tenant key")
        if self.token_limit <= 0 or self.window_seconds <= 0 or self.max_request_bytes <= 0:
            raise ConfigError("limits must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RouterConfig:
        env = os.environ if env is None else env

        def route(name: str) -> RouteConfig:
            p = name.upper()
            return RouteConfig(
                name=name,
                url=env.get(f"{p}_URL", ""),
                model=env.get(f"{p}_MODEL", ""),
                api_key=env.get(f"{p}_API_KEY") or None,
                timeout_ms=int(env.get(f"{p}_TIMEOUT_MS", "3000")),
            )

        try:
            return cls(
                tenant_keys=frozenset(k.strip() for k in env.get("TENANT_KEYS", "").split(",") if k.strip()),
                primary=route("primary"),
                secondary=route("secondary"),
                db_path=env.get("ROUTER_DB_PATH", ".state/router.db"),
                token_limit=int(env.get("TOKEN_LIMIT", "50000")),
                window_seconds=float(env.get("WINDOW_SECONDS", "60")),
                max_request_bytes=int(env.get("MAX_REQUEST_BYTES", "1048576")),
                db_busy_timeout_ms=int(env.get("DB_BUSY_TIMEOUT_MS", "5000")),
            )
        except ConfigError:
            raise
        except ValueError as exc:
            raise ConfigError(f"invalid number in configuration: {exc}") from exc
