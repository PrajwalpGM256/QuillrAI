"""Guardrail configuration from the environment. Invalid configuration refuses to start."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GuardrailConfig:
    upstream_url: str
    upstream_api_key: str | None = None
    upstream_timeout: float = 60.0
    max_request_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.upstream_url.startswith(("http://", "https://")):
            raise ConfigError("UPSTREAM_URL must be an http(s) URL")
        if self.upstream_timeout <= 0 or self.max_request_bytes <= 0:
            raise ConfigError("limits must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GuardrailConfig:
        env = os.environ if env is None else env
        try:
            return cls(
                upstream_url=env.get("UPSTREAM_URL", ""),
                upstream_api_key=env.get("UPSTREAM_API_KEY") or None,
                upstream_timeout=float(env.get("UPSTREAM_TIMEOUT", "60")),
                max_request_bytes=int(env.get("MAX_REQUEST_BYTES", "1048576")),
            )
        except ValueError as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(str(exc)) from exc
