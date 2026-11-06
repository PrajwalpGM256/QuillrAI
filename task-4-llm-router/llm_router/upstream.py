"""Primary/secondary routing with a cancellation-safe timeout race.

Fallback policy:

    primary 200 (JSON object)       -> return it
    primary 429                     -> fail over to the secondary
    primary no response in time     -> cancel the primary, wait until it is done, fail over
    primary other 4xx               -> 400 upstream_rejected_request, no retry
    primary 5xx / connection error  -> 502 upstream_error, no retry
    primary 200 with a bad body     -> 502 upstream_error, no retry

    secondary 200 (JSON object)     -> return it
    secondary 429                   -> 503 upstream_rate_limited
    secondary no response in time   -> 503 upstream_timeout
    secondary anything else         -> 503 upstream_unavailable

Only 429 and the timeout fail over. Other statuses mean the request itself or the provider is
broken, and sending a non-idempotent, billed request to a second provider would not fix that.

Cancellation isolation: each attempt runs as an asyncio.Task raced against its timeout with
asyncio.wait. On timeout the task is cancelled and then awaited until it is actually done before the
secondary starts, so the primary never keeps running (holding a connection, possibly completing and
billing) in the background. If the whole request is cancelled, the same cleanup runs in a finally.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from llm_router.config import RouteConfig
from llm_router.errors import GatewayError

logger = logging.getLogger(__name__)

Observer = Callable[[str, "asyncio.Task[Any] | None"], None]


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class TimedOut:
    """Sentinel: the attempt did not finish within its route timeout."""


TIMED_OUT = TimedOut()


@dataclass(frozen=True)
class Completion:
    payload: dict[str, Any]
    total_tokens: int | None
    route: str  # for logs only


class Router:
    def __init__(self, client: httpx2.AsyncClient, primary: RouteConfig, secondary: RouteConfig) -> None:
        self.client = client
        self.primary = primary
        self.secondary = secondary
        # Test hook: called with ("primary_start" | "primary_done" | "primary_cancelled" | "secondary_start"
        # | "secondary_done" | "secondary_cancelled", task)
        self.observer: Observer | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    def _notify(self, event: str, task: asyncio.Task[Any] | None) -> None:
        if self.observer is not None:
            self.observer(event, task)

    async def _call(self, route: RouteConfig, body: dict[str, Any]) -> Response:
        payload = {**body, "model": route.model}
        headers = {"content-type": "application/json", "accept": "application/json"}
        if route.api_key:
            headers["authorization"] = f"Bearer {route.api_key}"
        # httpx2's timeout is only a safety net; the asyncio race below is the authoritative deadline.
        safety = route.timeout_ms / 1000 + 5
        r = await self.client.post(route.url, json=payload, headers=headers, timeout=safety)
        return Response(r.status_code, r.content)

    async def _race(self, route: RouteConfig, body: dict[str, Any]) -> Response | TimedOut:
        """Run one attempt as a Task; on timeout cancel it and wait until it is done."""
        task = asyncio.create_task(self._call(route, body), name=f"upstream-{route.name}")
        self._notify(f"{route.name}_start", task)
        try:
            done, _ = await asyncio.wait({task}, timeout=route.timeout_ms / 1000)
            if task in done:
                self._notify(f"{route.name}_done", task)
                return task.result()  # re-raises transport errors
            task.cancel()
            await asyncio.wait({task})  # returns only once the task has finished cancelling
            self._notify(f"{route.name}_cancelled", task)
            return TIMED_OUT
        finally:
            if not task.done():  # the request itself was cancelled while we waited
                task.cancel()
                await asyncio.wait({task})
                self._notify(f"{route.name}_cancelled", task)

    @staticmethod
    def _completion(response: Response, route: str) -> Completion | None:
        try:
            payload = json.loads(response.body)
        except ValueError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
            return None
        usage = payload.get("usage")
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            total = None
        return Completion(payload, total, route)

    async def complete(self, body: dict[str, Any]) -> Completion:
        try:
            first = await self._race(self.primary, body)
        except httpx2.HTTPError as exc:
            logger.warning("primary transport error: %s", type(exc).__name__)
            raise GatewayError("upstream_error") from None

        if isinstance(first, Response):
            if first.status == 200:
                completion = self._completion(first, "primary")
                if completion is None:
                    raise GatewayError("upstream_error")
                return completion
            if first.status != 429:
                logger.warning("primary returned %d; not retried", first.status)
                raise GatewayError(
                    "upstream_rejected_request" if 400 <= first.status < 500 else "upstream_error"
                )
            logger.info("primary returned 429; failing over")
        else:
            logger.info("primary timed out after %d ms; failing over", self.primary.timeout_ms)

        try:
            second = await self._race(self.secondary, body)
        except httpx2.HTTPError as exc:
            logger.warning("secondary transport error: %s", type(exc).__name__)
            raise GatewayError("upstream_unavailable") from None
        if isinstance(second, TimedOut):
            raise GatewayError("upstream_timeout")
        if second.status == 429:
            raise GatewayError("upstream_rate_limited")
        if second.status != 200:
            logger.warning("secondary returned %d", second.status)
            raise GatewayError("upstream_unavailable")
        completion = self._completion(second, "secondary")
        if completion is None:
            raise GatewayError("upstream_unavailable")
        return completion
