"""The proxy end to end (in process): streaming and non-streaming redaction through the mock provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest

from llm_guardrail.app import build_app
from llm_guardrail.config import GuardrailConfig
from llm_guardrail.redactor import redact_text
from llm_guardrail.sse import SSEParser
from mock_provider.server import DEFAULT_TEXT, build_provider

pytestmark = pytest.mark.anyio

UPSTREAM_KEY = "gateway-upstream-key"
CLIENT_TOKEN = "client-secret-token"
SAMPLE = "Hi é, SSN 123-45-6789, card 4111-1111-1111-1111, mail first.last+x@mail.example.co.uk 😀"
SAMPLE_EXPECTED = "Hi é, SSN [REDACTED], card [REDACTED], mail [REDACTED] 😀"


@dataclass
class Recorder:
    """ASGI middleware that records the headers of every request the provider receives."""

    app: Any
    headers: list[dict[str, str]] = field(default_factory=list)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            self.headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


def config() -> GuardrailConfig:
    return GuardrailConfig(upstream_url="http://provider/v1/chat/completions", upstream_api_key=UPSTREAM_KEY)


@pytest.fixture
async def proxy() -> AsyncIterator[tuple[httpx2.AsyncClient, Recorder]]:
    recorder = Recorder(build_provider())
    app = build_app(config(), upstream_transport=httpx2.ASGITransport(app=recorder))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://guardrail"
    ) as client:
        yield client, recorder
    await app.state.client.aclose()


def chat(mock: dict[str, Any] | None = None, *, stream: bool = True, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": "my ssn is 123-45-6789"}],
    }
    body["stream"] = stream
    if mock is not None:
        body["mock"] = mock
    body.update(extra)
    return body


def events(raw: bytes) -> list[str]:
    return [e.data for e in SSEParser().feed(raw) if e.data is not None]


def contents(raw: bytes) -> dict[int, str]:
    """Join delta.content per choice index; every event must be valid JSON or [DONE]."""
    out: dict[int, str] = {}
    for data in events(raw):
        if data == "[DONE]":
            continue
        payload = json.loads(data)
        for choice in payload.get("choices", []):
            text = (choice.get("delta") or {}).get("content")
            if text:
                out[choice["index"]] = out.get(choice["index"], "") + text
    return out


async def post(client: httpx2.AsyncClient, body: dict[str, Any], **headers: str) -> httpx2.Response:
    return await client.post("/v1/chat/completions", json=body, headers=headers)


# --- happy paths --------------------------------------------------------------------------------


async def test_stream_redacts_default_reply(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat())
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert contents(r.content) == {0: redact_text(DEFAULT_TEXT)}
    assert contents(r.content)[0].count("[REDACTED]") == 3
    assert events(r.content)[-1] == "[DONE]"


async def test_non_stream_uses_the_same_redactor(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"text": SAMPLE}, stream=False))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == SAMPLE_EXPECTED


async def test_every_two_chunk_split_through_the_mock(proxy) -> None:
    """The adversarial provider cuts SAMPLE at every character offset; output never changes."""
    client, _ = proxy
    for i in range(len(SAMPLE) + 1):
        r = await post(client, chat({"text": SAMPLE, "splits": [i]}))
        assert contents(r.content) == {0: SAMPLE_EXPECTED}, f"split at char {i}"


async def test_one_character_chunks_through_the_mock(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"text": SAMPLE, "chunk_size": 1}))
    assert contents(r.content) == {0: SAMPLE_EXPECTED}


async def test_choices_have_separate_redactors(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"text": SAMPLE, "chunk_size": 3, "n": 2}))
    assert contents(r.content) == {0: SAMPLE_EXPECTED, 1: SAMPLE_EXPECTED}


async def test_missing_finish_reason_is_flushed_at_done(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"text": "mail a@b.co", "chunk_size": 2, "no_finish": True}))
    assert contents(r.content) == {0: "mail [REDACTED]"}
    assert events(r.content)[-1] == "[DONE]"


async def test_metadata_is_preserved(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"text": "hello", "chunk_size": 2}))
    first = json.loads(events(r.content)[0])
    assert first["id"] == "chatcmpl-mock" and first["model"] == "mock-1"
    assert first["choices"][0]["delta"]["role"] == "assistant"


# --- credentials --------------------------------------------------------------------------------


async def test_client_credential_never_forwarded(proxy) -> None:
    client, recorder = proxy
    await post(client, chat(), Authorization=f"Bearer {CLIENT_TOKEN}", Cookie="session=abc")
    sent = recorder.headers[-1]
    assert sent["authorization"] == f"Bearer {UPSTREAM_KEY}"
    assert CLIENT_TOKEN not in json.dumps(sent) and "cookie" not in sent


# --- fail closed --------------------------------------------------------------------------------


async def test_tools_rejected_and_never_forwarded(proxy) -> None:
    client, recorder = proxy
    for key in ("tools", "functions"):
        r = await post(client, chat(**{key: [{"type": "function", "function": {"name": "f"}}]}))
        assert r.status_code == 400 and r.json()["error"]["code"] == "tools_not_supported"
    assert recorder.headers == []


@pytest.mark.parametrize(("raw", "code"), [(b"{not json", "malformed_json"), (b"[1,2]", "not_an_object")])
async def test_bad_request_bodies(proxy, raw: bytes, code: str) -> None:
    client, recorder = proxy
    r = await client.post("/v1/chat/completions", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert recorder.headers == []


async def test_upstream_error_status_kept_body_replaced(proxy) -> None:
    client, _ = proxy
    r = await post(client, chat({"status": 429}))
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "upstream_status"
    assert "123-45-6789" not in r.text  # the mock echoed the prompt; the gateway did not


async def test_mid_stream_break_drops_held_tail() -> None:
    """The connection dies while '123-45-67' is held: that prefix must never be emitted.

    (httpx2's ASGITransport buffers whole responses, so a real mid-stream drop from the mock provider
    is tested over sockets in test_ttft.py; here the upstream stream raises a read error directly.)
    """

    class Breaks(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield sse(delta("ok 123-45-67"))
            raise httpx2.ReadError("connection reset")

    def handler(_req):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Breaks())

    r = await run_with_upstream(handler, chat())
    data = events(r.content)
    assert "[DONE]" not in data
    assert json.loads(data[-1])["error"]["code"] == "stream_failed"
    assert contents(r.content) == {0: "ok "}
    assert "123" not in r.text


def transport_with(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)


class Chunks(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


async def run_with_upstream(handler, body: dict[str, Any]) -> httpx2.Response:
    app = build_app(config(), upstream_transport=transport_with(handler))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://g") as client:
        r = await post(client, body)
    await app.state.client.aclose()
    return r


def sse(*payloads: Any) -> bytes:
    out = b""
    for p in payloads:
        data = p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
        out += b"data: " + data.encode("utf-8") + b"\n\n"
    return out


def delta(text: str, finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "x",
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }


async def test_transport_split_inside_multibyte_characters() -> None:
    """Raw upstream bytes cut at every offset, including inside 'é', '€', '😀' and inside 'data:'."""
    stream = sse(delta("é 123-45-"), delta("6789 € a@b.co"), delta(" 😀", "stop"), "[DONE]")
    for i in range(len(stream) + 1):

        def handler(_req, i=i):
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Chunks([stream[:i], stream[i:]])
            )

        r = await run_with_upstream(handler, chat())
        assert contents(r.content) == {0: "é [REDACTED] € [REDACTED] 😀"}, f"split at byte {i}"


@pytest.mark.parametrize(
    "bad",
    [
        sse(delta("ok 123-45-67"), "{not json"),
        sse(delta("ok 123-45-67"), {"error": {"message": "boom"}}),
        sse(delta("ok 123-45-67")),  # ends without [DONE]
        sse(delta("ok 123-45-67"), {"choices": [{"index": 0, "delta": {"tool_calls": [{"id": "t"}]}}]}),
    ],
    ids=["malformed_json", "error_event", "no_done", "tool_calls"],
)
async def test_malformed_upstream_streams_fail_closed(bad: bytes) -> None:
    def handler(_req):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([bad]))

    r = await run_with_upstream(handler, chat())
    data = events(r.content)
    assert json.loads(data[-1])["error"]["code"] == "stream_failed"
    assert "[DONE]" not in data
    assert contents(r.content) == {0: "ok "} and "123" not in r.text


async def test_upstream_unreachable_and_timeout() -> None:
    def refuse(_req):
        raise httpx2.ConnectError("refused")

    def slow(_req):
        raise httpx2.ReadTimeout("slow")

    assert (await run_with_upstream(refuse, chat())).status_code == 502
    assert (await run_with_upstream(slow, chat())).status_code == 504


async def test_stream_requested_but_upstream_sends_json() -> None:
    def handler(_req):
        return httpx2.Response(200, json={"choices": []})

    r = await run_with_upstream(handler, chat())
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_not_streaming"


async def test_oversized_request_rejected_before_forwarding() -> None:
    recorder = Recorder(build_provider())
    small = GuardrailConfig(upstream_url="http://provider/v1/chat/completions", max_request_bytes=64)
    app = build_app(small, upstream_transport=httpx2.ASGITransport(app=recorder))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://g") as client:
        r = await post(client, chat({"text": "x" * 200}))
    await app.state.client.aclose()
    assert r.status_code == 413 and recorder.headers == []
