"""The guardrail proxy: POST /v1/chat/completions, forwarded upstream, PII redacted in the response.

Streaming responses are parsed event by event. Each choice index gets its own StreamRedactor; each
upstream event is re-emitted 1:1 carrying only the text that has passed its settlement point. When a
choice finishes, its held tail is flushed through finish() into that same event.

Fail closed:
- A non-200 upstream response keeps its status, but the body is replaced by a gateway error, because
  upstream error bodies can echo prompt text.
- A broken, malformed, or truncated stream drops every held tail (it may be the start of a PII value),
  emits one SSE error event, and closes without [DONE].
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from llm_guardrail.config import GuardrailConfig
from llm_guardrail.redactor import StreamRedactor, redact
from llm_guardrail.sse import SSEParser, encode_data

logger = logging.getLogger(__name__)

META_KEYS = ("id", "object", "created", "model", "system_fingerprint")


class StreamFailure(Exception):
    """The upstream stream cannot be relayed safely."""


def error_body(message: str, kind: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "code": code}}


def _error(status: int, message: str, kind: str, code: str) -> JSONResponse:
    return JSONResponse(error_body(message, kind, code), status_code=status)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _encode_content(content: str) -> bytes:
    # A lone surrogate (half of a pair split across deltas) has no UTF-8 form; it decodes to U+FFFD.
    return content.encode("utf-8", errors="surrogatepass")


def redact_completion(body: Any) -> Any:
    """Non-streaming path: redact each choices[].message.content with the same StreamRedactor."""
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        raise StreamFailure("unexpected completion shape")
    for choice in body["choices"]:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise StreamFailure("choice without a message")
        if message.get("tool_calls") or message.get("function_call"):
            raise StreamFailure("tool calls are not supported")
        content = message.get("content")
        if content is None:
            continue
        if not isinstance(content, str):
            raise StreamFailure("non-string content")
        message["content"] = redact(_encode_content(content)).decode("utf-8")
    return body


class ChoiceRedactors:
    """One StreamRedactor per choice index, plus the event rewriting logic."""

    def __init__(self) -> None:
        self._redactors: dict[int, StreamRedactor] = {}
        self._finished: set[int] = set()
        self._meta: dict[str, Any] = {}

    def rewrite(self, event: dict[str, Any]) -> dict[str, Any]:
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise StreamFailure("event without choices")
        self._meta = {k: event[k] for k in META_KEYS if k in event}
        for choice in choices:
            if not isinstance(choice, dict):
                raise StreamFailure("malformed choice")
            index = choice.get("index", 0)
            if not isinstance(index, int) or isinstance(index, bool) or index in self._finished:
                raise StreamFailure("bad or finished choice index")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise StreamFailure("malformed delta")
            if delta.get("tool_calls") or delta.get("function_call"):
                raise StreamFailure("tool calls are not supported")
            redactor = self._redactors.setdefault(index, StreamRedactor())
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise StreamFailure("non-string content")
            out = redactor.feed(_encode_content(content)) if content else b""
            if choice.get("finish_reason") is not None:
                out += redactor.finish()
                self._finished.add(index)
            if content is not None or out:
                delta["content"] = out.decode("utf-8")
                choice["delta"] = delta
        return event

    def flush_unfinished(self) -> dict[str, Any] | None:
        """At [DONE]: flush any choice that never sent finish_reason into one synthetic chunk."""
        choices = []
        for index, redactor in sorted(self._redactors.items()):
            if index in self._finished:
                continue
            self._finished.add(index)
            tail = redactor.finish()
            if tail:
                choices.append(
                    {"index": index, "delta": {"content": tail.decode("utf-8")}, "finish_reason": None}
                )
        if not choices:
            return None
        return {**self._meta, "object": "chat.completion.chunk", "choices": choices}

    @property
    def all_finished(self) -> bool:
        return all(i in self._finished for i in self._redactors)


async def relay_stream(upstream: httpx2.Response) -> AsyncIterator[bytes]:
    parser = SSEParser()
    choices = ChoiceRedactors()
    try:
        async for chunk in upstream.aiter_raw():
            for event in parser.feed(chunk):
                if event.data is None:
                    continue
                if event.data == "[DONE]":
                    flushed = choices.flush_unfinished()
                    if flushed is not None:
                        yield encode_data(_dumps(flushed))
                    yield encode_data("[DONE]")
                    return
                try:
                    payload = json.loads(event.data)
                except ValueError as exc:
                    raise StreamFailure("malformed event JSON") from exc
                if not isinstance(payload, dict):
                    raise StreamFailure("event is not an object")
                if "error" in payload:
                    raise StreamFailure("upstream reported an error mid-stream")
                yield encode_data(_dumps(choices.rewrite(payload)))
        raise StreamFailure("upstream ended without [DONE]")
    except Exception as exc:  # fail closed on anything: held tails are dropped, never flushed
        logger.warning("stream failed closed: %s", exc)
        yield encode_data(
            _dumps(error_body("upstream stream failed; output truncated", "upstream_error", "stream_failed"))
        )
    finally:
        await upstream.aclose()


def build_app(
    config: GuardrailConfig, *, upstream_transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    client = httpx2.AsyncClient(transport=upstream_transport, timeout=config.upstream_timeout)

    async def chat_completions(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > config.max_request_bytes:
            return _error(413, "request body too large", "invalid_request_error", "body_too_large")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "request body must be JSON", "invalid_request_error", "malformed_json")
        if not isinstance(body, dict):
            return _error(400, "request body must be a JSON object", "invalid_request_error", "not_an_object")
        if "tools" in body or "functions" in body:
            # tool_call arguments would be an unredacted channel, so refuse rather than leak
            return _error(
                400, "tools are not supported by this gateway", "invalid_request_error", "tools_not_supported"
            )
        stream = body.get("stream") is True

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        }
        if config.upstream_api_key:
            headers["authorization"] = (
                f"Bearer {config.upstream_api_key}"  # the client's credential is never sent
            )
        outbound = client.build_request("POST", config.upstream_url, content=raw, headers=headers)
        try:
            upstream = await client.send(outbound, stream=True)
        except httpx2.TimeoutException:
            return _error(504, "upstream timed out", "upstream_error", "upstream_timeout")
        except Exception as exc:  # transport errors and anything else: fail closed, no detail leaked
            logger.warning("upstream request failed: %s", exc)
            return _error(502, "upstream unavailable", "upstream_error", "upstream_unavailable")

        if upstream.status_code != 200:
            await upstream.aclose()
            return _error(
                upstream.status_code, "upstream returned an error", "upstream_error", "upstream_status"
            )

        content_type = upstream.headers.get("content-type", "")
        if stream:
            if not content_type.startswith("text/event-stream"):
                await upstream.aclose()
                return _error(502, "upstream did not stream", "upstream_error", "upstream_not_streaming")
            return StreamingResponse(
                relay_stream(upstream),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            payload = json.loads(await upstream.aread())
            return JSONResponse(redact_completion(payload))
        except (ValueError, StreamFailure):
            return _error(
                502, "upstream returned an unusable completion", "upstream_error", "upstream_malformed"
            )
        finally:
            await upstream.aclose()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    app = Starlette(
        routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])], lifespan=lifespan
    )
    app.state.client = client
    return app
