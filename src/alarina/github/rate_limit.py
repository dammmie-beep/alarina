from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 300.0
_RESET_BUFFER_SECS = 2.0
# 429 / 5xx are unconditionally retryable; 403 needs extra checks (see _is_retryable)
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503})


@dataclass
class RateLimitState:
    remaining: int = 5000
    reset_at: float = field(default_factory=time.time)

    def update_from_headers(self, headers: httpx.Headers) -> None:
        if (r := headers.get("X-RateLimit-Remaining")) is not None:
            self.remaining = int(r)
        if (t := headers.get("X-RateLimit-Reset")) is not None:
            self.reset_at = float(t)

    def update_from_graphql(self, rate_limit: dict[str, object]) -> None:
        if (r := rate_limit.get("remaining")) is not None:
            self.remaining = int(r)  # type: ignore[arg-type]

    async def wait_if_exhausted(self) -> None:
        if self.remaining == 0:
            delay = max(0.0, self.reset_at - time.time()) + _RESET_BUFFER_SECS
            logger.debug("Rate limit exhausted; sleeping %.1f s until reset.", delay)
            await asyncio.sleep(delay)


def _is_retryable(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code in _RETRYABLE_STATUSES:
        return True
    # GitHub secondary rate limit: 403 with Retry-After or remaining=0 header
    if exc.response.status_code == 403:
        h = exc.response.headers
        return h.get("Retry-After") is not None or h.get("X-RateLimit-Remaining") == "0"
    return False


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    state: RateLimitState,
    max_retries: int = 6,
) -> T:
    for attempt in range(max_retries + 1):
        await state.wait_if_exhausted()
        try:
            return await coro_fn()
        except httpx.HTTPStatusError as exc:
            if not _is_retryable(exc) or attempt == max_retries:
                raise
            await _backoff(attempt, exc.response.headers.get("Retry-After"))
        except httpx.TransportError:
            if attempt == max_retries:
                raise
            await _backoff(attempt, None)
    raise RuntimeError("unreachable")  # pragma: no cover


async def _backoff(attempt: int, retry_after: str | None) -> None:
    if retry_after is not None:
        delay = float(retry_after)
        logger.debug("Retry-After: sleeping %.1f s.", delay)
    else:
        delay = min(_BACKOFF_BASE * (2**attempt) + random.random(), _BACKOFF_CAP)
        logger.debug("Backoff attempt %d: sleeping %.2f s.", attempt, delay)
    await asyncio.sleep(delay)
