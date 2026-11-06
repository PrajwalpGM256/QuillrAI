"""In-process test stack: the router app wired to two mock providers by hostname."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx2

from llm_router.app import build_app
from llm_router.clock import Clock
from llm_router.config import RouteConfig, RouterConfig
from llm_router.tokens import BYTES_PER_TOKEN, PER_MESSAGE_OVERHEAD
from mock_provider.server import build_provider

KEY_A = "tenant-a-key-9d2f"
KEY_B = "tenant-b-key-41c7"
PRIMARY_MODEL = "primary-model-x41"
SECONDARY_MODEL = "secondary-model-y77"
PRIMARY_UPSTREAM_KEY = "upstream-primary-key-55"
SECONDARY_UPSTREAM_KEY = "upstream-secondary-key-66"


def make_config(db_path: Path | str, **overrides: Any) -> RouterConfig:
    primary_timeout = overrides.pop("primary_timeout_ms", 200)
    secondary_timeout = overrides.pop("secondary_timeout_ms", 200)
    config = RouterConfig(
        tenant_keys=frozenset({KEY_A, KEY_B}),
        primary=RouteConfig(
            "primary",
            "http://primary/v1/chat/completions",
            PRIMARY_MODEL,
            PRIMARY_UPSTREAM_KEY,
            primary_timeout,
        ),
        secondary=RouteConfig(
            "secondary",
            "http://secondary/v1/chat/completions",
            SECONDARY_MODEL,
            SECONDARY_UPSTREAM_KEY,
            secondary_timeout,
        ),
        db_path=str(db_path),
    )
    return replace(config, **overrides)


def body_with_tokens(n: int, **extra: Any) -> dict[str, Any]:
    """A request whose estimate is exactly ``n`` tokens."""
    content = "x" * (BYTES_PER_TOKEN * (n - PER_MESSAGE_OVERHEAD))
    return {"model": "client-choice", "messages": [{"role": "user", "content": content}], **extra}


class HostTransport(httpx2.AsyncBaseTransport):
    """Routes http://primary and http://secondary to in-process apps; hosts in ``down`` refuse."""

    def __init__(self, apps: dict[str, Any]) -> None:
        self.transports = {host: httpx2.ASGITransport(app=app) for host, app in apps.items()}
        self.down: set[str] = set()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        if host in self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        return await self.transports[host].handle_async_request(request)


@dataclass
class Stack:
    client: httpx2.AsyncClient
    app: Any
    primary: Any
    secondary: Any
    transport: HostTransport
    events: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def limiter(self):
        return self.app.state.limiter

    @property
    def router(self):
        return self.app.state.router

    def set(self, which: str, **behaviour: Any) -> None:
        target = getattr(self, which).state.behaviour
        for key, value in behaviour.items():
            setattr(target, key, value)

    def stats(self, which: str):
        return getattr(self, which).state.stats

    async def chat(self, body: dict[str, Any] | None = None, *, key: str | None = KEY_A, **headers: str):
        hdrs = {"content-type": "application/json", **headers}
        if key is not None:
            hdrs["authorization"] = f"Bearer {key}"
        return await self.client.post(
            "/v1/chat/completions", json=body or body_with_tokens(100), headers=hdrs
        )


@contextlib.asynccontextmanager
async def running(config: RouterConfig, clock: Clock | None = None) -> AsyncIterator[Stack]:
    primary, secondary = build_provider("primary"), build_provider("secondary")
    transport = HostTransport({"primary": primary, "secondary": secondary})
    app = build_app(config, clock=clock, transport=transport)
    async with app.router.lifespan_context(app):
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://router", timeout=30
        )
        stack = Stack(client, app, primary, secondary, transport)
        stack.router.observer = lambda event, task: stack.events.append((event, task))
        try:
            yield stack
        finally:
            await client.aclose()
