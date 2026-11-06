"""Mock OpenAI-compatible provider whose behaviour is set at runtime.

    POST /v1/chat/completions   answers according to the current behaviour
    POST /_mock                 set behaviour: {"status", "delay_ms", "usage_total", "malformed"}
    POST /_mock/reset           default behaviour, zeroed stats
    GET  /_mock/stats           {"requests", "completed", "cancelled", "disconnected", "auth"}

Error bodies deliberately contain a canary string, the mock's own URL and model name, and a fake
traceback, so tests can prove none of it ever reaches a router client.

While delaying, the handler polls for client disconnect (real sockets) and also records task
cancellation (in-process transports), so a primary the router abandoned is observable.

Run standalone:  PORT=9201 MOCK_NAME=primary uv run python -m mock_provider.server
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

CANARY = "MOCK-UPSTREAM-CANARY-7f3a"


@dataclass
class Behaviour:
    status: int = 200
    delay_ms: int = 0
    usage_total: int | None = None  # None: report prompt estimate-ish usage
    malformed: bool = False


@dataclass
class Stats:
    requests: int = 0
    completed: int = 0
    cancelled: int = 0
    disconnected: int = 0
    auth: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)


def build_provider(name: str = "mock") -> Starlette:
    behaviour = Behaviour()
    stats = Stats()

    async def completions(request: Request) -> Response:
        stats.requests += 1
        stats.auth.append(request.headers.get("authorization", ""))
        body = await request.json()
        stats.models.append(str(body.get("model")))
        b = behaviour
        try:
            deadline = time.monotonic() + b.delay_ms / 1000
            while time.monotonic() < deadline:
                await asyncio.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
                if await request.is_disconnected():
                    stats.disconnected += 1
                    return Response(status_code=499)
        except asyncio.CancelledError:
            stats.cancelled += 1
            raise
        if b.status != 200:
            return JSONResponse(
                {
                    "error": {
                        "message": f"{CANARY} {name} failure at {request.url} for model {body.get('model')}",
                        "trace": 'Traceback (most recent call last):\n  File "provider.py", line 1',
                    }
                },
                status_code=b.status,
            )
        if b.malformed:
            return Response(f"<html>{CANARY}</html>", media_type="text/html")
        total = b.usage_total if b.usage_total is not None else 42
        stats.completed += 1
        return JSONResponse(
            {
                "id": f"chatcmpl-{name}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"hello from {name}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": total // 2,
                    "completion_tokens": total - total // 2,
                    "total_tokens": total,
                },
            }
        )

    async def set_behaviour(request: Request) -> Response:
        data: dict[str, Any] = await request.json()
        for key, value in data.items():
            if hasattr(behaviour, key):
                setattr(behaviour, key, value)
        return JSONResponse(asdict(behaviour))

    async def reset(_request: Request) -> Response:
        fresh_b, fresh_s = Behaviour(), Stats()
        for k, v in asdict(fresh_b).items():
            setattr(behaviour, k, v)
        stats.__dict__.update(asdict(fresh_s))
        return JSONResponse({"ok": True})

    async def get_stats(_request: Request) -> Response:
        return JSONResponse(asdict(stats))

    app = Starlette(
        routes=[
            Route("/v1/chat/completions", completions, methods=["POST"]),
            Route("/_mock", set_behaviour, methods=["POST"]),
            Route("/_mock/reset", reset, methods=["POST"]),
            Route("/_mock/stats", get_stats, methods=["GET"]),
        ]
    )
    app.state.behaviour = behaviour
    app.state.stats = stats
    return app


def main() -> None:
    import uvicorn

    app = build_provider(os.environ.get("MOCK_NAME", "mock"))
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("PORT", "9201")), log_config=None, access_log=False
    )


if __name__ == "__main__":
    main()
