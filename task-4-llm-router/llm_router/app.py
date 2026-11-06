"""The router app: POST /v1/chat/completions.

Pipeline (each failure returns the standard error schema and never reaches an upstream):
  1. Authenticate the Bearer key against TENANT_KEYS           -> 401
  2. Body size, JSON object, messages, no streaming            -> 413 / 400
  3. Estimate input tokens; more than the whole window budget  -> 400 token_budget_exceeded
  4. Reserve the estimate in the sliding window                -> 429 rate_limited + Retry-After
  5. Route: primary, failing over on 429 or timeout            -> see upstream.py
  6. Settle the actual usage on success; release on any failure, including cancellation
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llm_router.clock import Clock
from llm_router.config import RouterConfig
from llm_router.errors import GatewayError, error_body, error_response
from llm_router.limiter import LimiterUnavailable, Rejected, SqliteWindowLimiter
from llm_router.tokens import InvalidMessages, estimate_tokens
from llm_router.upstream import Router

logger = logging.getLogger(__name__)

_BEARER = re.compile(r"(?i:bearer) ([A-Za-z0-9\-._~+/]+=*)")


def tenant_id(key: str) -> str:
    """Stored in SQLite instead of the key itself."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def fingerprint(key: str) -> str:
    """Logged instead of the key."""
    return tenant_id(key)[:8]


def authenticate(values: list[str], keys: frozenset[str]) -> str:
    if not values:
        raise GatewayError("missing_api_key")
    match = _BEARER.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise GatewayError("invalid_api_key")
    presented = match.group(1).encode("utf-8")
    found: str | None = None
    for key in keys:  # compare against every key so timing does not reveal which matched
        if hmac.compare_digest(presented, key.encode("utf-8")):
            found = key
    if found is None:
        raise GatewayError("invalid_api_key")
    return found


def build_app(
    config: RouterConfig,
    *,
    clock: Clock | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Starlette:
    limiter = SqliteWindowLimiter(
        config.db_path,
        limit=config.token_limit,
        window_seconds=config.window_seconds,
        clock=clock,
        busy_timeout_ms=config.db_busy_timeout_ms,
    )
    router = Router(httpx2.AsyncClient(transport=transport), config.primary, config.secondary)

    async def parse_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > config.max_request_bytes:
            raise GatewayError("request_too_large")
        try:
            body = json.loads(raw)
        except ValueError:
            raise GatewayError("malformed_json") from None
        if not isinstance(body, dict):
            raise GatewayError("malformed_json")
        if body.get("stream") is True:
            raise GatewayError("streaming_not_supported")
        return body

    async def handle(request: Request, request_id: str) -> Response:
        key = authenticate(request.headers.getlist("authorization"), config.tenant_keys)
        fp, tenant = fingerprint(key), tenant_id(key)
        body = await parse_body(request)
        try:
            estimate = estimate_tokens(body.get("messages"))
        except InvalidMessages:
            raise GatewayError("invalid_messages") from None
        if estimate > config.token_limit:
            raise GatewayError("token_budget_exceeded")

        try:
            decision = await limiter.reserve(tenant, estimate)
        except LimiterUnavailable:
            logger.error("request_id=%s limiter unavailable; refusing (fail closed)", request_id)
            raise GatewayError("limiter_unavailable") from None
        if isinstance(decision, Rejected):
            logger.info("request_id=%s tenant=%s rate limited (estimate=%d)", request_id, fp, estimate)
            raise GatewayError("rate_limited", retry_after=decision.retry_after)

        try:
            completion = await router.complete(body)
        except BaseException:
            # Any failure, including cancellation of this request, gives the reservation back.
            try:
                await limiter.release(decision)
            except LimiterUnavailable:
                logger.error("request_id=%s release failed; reservation expires with the window", request_id)
            raise

        charged = completion.total_tokens if completion.total_tokens is not None else estimate
        try:
            await limiter.settle(decision, charged)
        except LimiterUnavailable:
            logger.error("request_id=%s settle failed; the reservation stays charged", request_id)
        logger.info(
            "request_id=%s tenant=%s route=%s estimate=%d charged=%d",
            request_id,
            fp,
            completion.route,
            estimate,
            charged,
        )
        return JSONResponse(completion.payload, headers={"X-Request-ID": request_id})

    async def chat_completions(request: Request) -> Response:
        request_id = uuid.uuid4().hex
        try:
            return await handle(request, request_id)
        except GatewayError as exc:
            return error_response(exc, request_id)
        except Exception:
            logger.exception("request_id=%s unhandled error", request_id)  # traceback stays server-side
            return error_response(GatewayError("internal_error"), request_id)

    async def http_exception(_request: Request, exc: Exception) -> Response:
        request_id = uuid.uuid4().hex
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            getattr(exc, "status_code", 500), "internal_error"
        )
        return error_response(GatewayError(code), request_id)

    async def unhandled(_request: Request, _exc: Exception) -> Response:
        request_id = uuid.uuid4().hex
        logger.exception("request_id=%s unhandled error outside the handler", request_id)
        return JSONResponse(
            error_body("internal_error", request_id), status_code=500, headers={"X-Request-ID": request_id}
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await limiter.start()
        try:
            yield
        finally:
            await router.aclose()
            await limiter.close()

    app = Starlette(
        routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])],
        exception_handlers={HTTPException: http_exception, Exception: unhandled},
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.state.router = router
    return app
