"""ASGI middleware that records every request reaching the downstream (headers and raw body)."""

from __future__ import annotations

from dataclasses import dataclass, field

from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass
class Recorded:
    method: str
    path: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]


@dataclass
class Spy:
    app: ASGIApp
    records: list[Recorded] = field(default_factory=list)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        record = Recorded(
            method=scope["method"],
            path=scope["path"],
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]],
        )
        self.records.append(record)

        async def recording_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                record.body += message.get("body", b"")
            return message

        await self.app(scope, recording_receive, send)

    def reset(self) -> None:
        self.records.clear()
