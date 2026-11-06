"""Adversarial OpenAI-compatible mock LLM provider.

POST /v1/chat/completions. A ``mock`` object in the request body controls the reply:

    text         full assistant reply (default: a sentence containing one SSN, card, and email)
    splits       character offsets where content chunks are cut, e.g. [5, 12]
    chunk_size   alternatively, cut every N characters (default 4)
    delay_ms     pause before each event (default 0)
    status       return this HTTP error status instead of a completion
    break_after  stream N content events, then drop the connection without [DONE]
    n            number of choices; choice i streams the same text (default 1)
    no_finish    omit finish_reason events (the stream still ends with [DONE])

Offsets are characters, not bytes: a JSON string cannot hold half of a UTF-8 character. Byte-level
splits inside a character are exercised at the transport level in the tests.

Run standalone:  PORT=9100 uv run python -m mock_provider.server
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

DEFAULT_TEXT = (
    "Sure. The SSN on file is 123-45-6789, the card is 4111 1111 1111 1111, "
    "and the contact email is jane.doe@example.com. Anything else?"
)


class DroppedConnection(Exception):
    """Raised mid-stream to simulate an upstream that dies without [DONE]."""


def chunk_text(text: str, splits: list[int] | None, chunk_size: int) -> list[str]:
    if splits is not None:
        points = sorted({s for s in splits if 0 < s < len(text)})
        edges = [0, *points, len(text)]
        return [text[a:b] for a, b in zip(edges, edges[1:], strict=False)]
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


def _chunk(cid: str, created: int, choices: list[dict[str, Any]]) -> bytes:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "mock-1",
        "choices": choices,
    }
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


async def chat_completions(request: Request) -> Response:
    body = await request.json()
    mock: dict[str, Any] = body.get("mock") or {}
    if "status" in mock:
        status = int(mock["status"])
        return JSONResponse(
            {"error": {"message": f"mock failure; prompt was: {body.get('messages')}", "type": "mock"}},
            status_code=status,
        )
    text = mock.get("text", DEFAULT_TEXT)
    n = int(mock.get("n", 1))
    created = int(time.time())
    cid = "chatcmpl-mock"

    if body.get("stream") is not True:
        choices = [
            {"index": i, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            for i in range(n)
        ]
        return JSONResponse(
            {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": "mock-1",
                "choices": choices,
            }
        )

    pieces = chunk_text(text, mock.get("splits"), int(mock.get("chunk_size", 4)))
    delay = float(mock.get("delay_ms", 0)) / 1000
    break_after = mock.get("break_after")
    no_finish = bool(mock.get("no_finish", False))

    async def events() -> AsyncIterator[bytes]:
        yield _chunk(
            cid,
            created,
            [
                {"index": i, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}
                for i in range(n)
            ],
        )
        for count, piece in enumerate(pieces):
            if break_after is not None and count >= int(break_after):
                raise DroppedConnection("mock dropped the connection")
            if delay:
                await asyncio.sleep(delay)
            for i in range(n):
                yield _chunk(cid, created, [{"index": i, "delta": {"content": piece}, "finish_reason": None}])
        if not no_finish:
            yield _chunk(cid, created, [{"index": i, "delta": {}, "finish_reason": "stop"} for i in range(n)])
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def build_provider() -> Starlette:
    return Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])


app = build_provider()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "9100")), log_config=None)


if __name__ == "__main__":
    main()
