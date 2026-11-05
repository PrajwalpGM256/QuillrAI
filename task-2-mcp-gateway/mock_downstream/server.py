"""Mock downstream: a real SDK MCPServer over Streamable HTTP, behind a spy and a credential check.

It trusts exactly one credential, the gateway's DOWNSTREAM_TOKEN. Tool names include traps for
the gateway's prefix policy (administrator_report is not an admin tool).

Run standalone:  DOWNSTREAM_TOKEN=... PORT=9000 uv run python -m mock_downstream.server
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from mock_downstream.spy import Spy


def build_mcp() -> MCPServer:
    mcp = MCPServer(name="mock-downstream", version="0.1.0")

    @mcp.tool()
    def get_weather(city: str) -> str:
        """Current weather for a city."""
        return f"Sunny in {city}"

    @mcp.tool()
    def administrator_report() -> str:
        """Public report. Starts with 'admin' but not 'admin_', so it is not an admin tool."""
        return "report"

    @mcp.tool()
    def admin_delete_user(user_id: str) -> str:
        """Admin-only: delete a user."""
        return f"deleted {user_id}"

    @mcp.tool()
    def admin_reset_cache() -> str:
        """Admin-only: reset caches."""
        return "cache reset"

    return mcp


class RequireToken:
    """Reject any request that does not carry exactly the gateway's downstream credential."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            auth = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
            if auth != [self.expected]:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def build_downstream(token: str, *, json_response: bool = True) -> tuple[Spy, Starlette]:
    """Return (spy, starlette_app). The spy is the ASGI app to serve; it wraps the credential check.

    Enter ``starlette_app.router.lifespan_context(starlette_app)`` to start the SDK session manager.
    """
    starlette_app = build_mcp().streamable_http_app(
        json_response=json_response,
        # Mock only: tests reach it via in-process transports with arbitrary Host headers.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Spy(RequireToken(starlette_app, token)), starlette_app


def main() -> None:
    import uvicorn

    token = os.environ["DOWNSTREAM_TOKEN"]
    app, starlette_app = build_downstream(token, json_response=os.environ.get("JSON_RESPONSE", "1") == "1")

    async def with_lifespan(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await starlette_app(scope, receive, send)
        else:
            await app(scope, receive, send)

    uvicorn.run(with_lifespan, host="127.0.0.1", port=int(os.environ.get("PORT", "9000")), log_config=None)


if __name__ == "__main__":
    main()
