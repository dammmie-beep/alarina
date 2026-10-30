"""Unit tests for rate-limit state tracking and retry logic.

All calls to asyncio.sleep are patched so tests run instantly.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, call, patch

import httpx
import pytest

from alarina.github.rate_limit import (
    RateLimitState,
    _backoff,
    _is_retryable,
    with_retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/graphql")
    response = httpx.Response(status, headers=headers or {})
    return httpx.HTTPStatusError("", request=request, response=response)


# ---------------------------------------------------------------------------
# RateLimitState
# ---------------------------------------------------------------------------


class TestRateLimitState:
    def test_update_from_headers_parses_both_fields(self) -> None:
        state = RateLimitState()
        state.update_from_headers(
            httpx.Headers({"X-RateLimit-Remaining": "42", "X-RateLimit-Reset": "1700000000"})
        )
        assert state.remaining == 42
        assert state.reset_at == 1_700_000_000.0

    def test_update_from_headers_ignores_absent_fields(self) -> None:
        state = RateLimitState(remaining=100, reset_at=999.0)
        state.update_from_headers(httpx.Headers({}))
        assert state.remaining == 100
        assert state.reset_at == 999.0

    def test_update_from_graphql_sets_remaining(self) -> None:
        state = RateLimitState()
        state.update_from_graphql({"remaining": 4321})
        assert state.remaining == 4321

    def test_update_from_graphql_ignores_absent_key(self) -> None:
        state = RateLimitState(remaining=500)
        state.update_from_graphql({})
        assert state.remaining == 500

    async def test_wait_if_exhausted_sleeps_until_reset(self) -> None:
        state = RateLimitState(remaining=0, reset_at=time.time() + 100.0)
        with patch("alarina.github.rate_limit.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await state.wait_if_exhausted()
        mock_sleep.assert_awaited_once()
        delay: float = mock_sleep.call_args[0][0]
        # delay = (reset_at - now) + 2s buffer ≈ 102s; allow small timing drift
        assert 100.0 < delay <= 104.0

    async def test_wait_if_exhausted_no_sleep_when_budget_remains(self) -> None:
        state = RateLimitState(remaining=1)
        with patch("alarina.github.rate_limit.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await state.wait_if_exhausted()
        mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# _is_retryable
# ---------------------------------------------------------------------------


class TestIsRetryable:
    def test_429_is_retryable(self) -> None:
        assert _is_retryable(_http_error(429))

    def test_500_is_retryable(self) -> None:
        assert _is_retryable(_http_error(500))

    def test_502_is_retryable(self) -> None:
        assert _is_retryable(_http_error(502))

    def test_403_with_retry_after_is_retryable(self) -> None:
        assert _is_retryable(_http_error(403, {"Retry-After": "30"}))

    def test_403_with_remaining_zero_is_retryable(self) -> None:
        assert _is_retryable(_http_error(403, {"X-RateLimit-Remaining": "0"}))

    def test_403_plain_auth_failure_is_not_retryable(self) -> None:
        assert not _is_retryable(_http_error(403))

    def test_404_is_not_retryable(self) -> None:
        assert not _is_retryable(_http_error(404))

    def test_422_is_not_retryable(self) -> None:
        assert not _is_retryable(_http_error(422))


# ---------------------------------------------------------------------------
# _backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    async def test_uses_retry_after_header_value(self) -> None:
        with patch("alarina.github.rate_limit.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _backoff(0, "45")
        mock_sleep.assert_awaited_once_with(45.0)

    async def test_exponential_growth_without_retry_after(self) -> None:
        delays: list[float] = []
        with patch("alarina.github.rate_limit.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            for attempt in range(4):
                await _backoff(attempt, None)
                delays.append(mock_sleep.call_args[0][0])
        # Each step should be roughly double the previous (jitter is ≤1s)
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1]

    async def test_capped_at_300s(self) -> None:
        with patch("alarina.github.rate_limit.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await _backoff(100, None)  # 2^100 >> cap
        assert mock_sleep.call_args[0][0] <= 300.0


# ---------------------------------------------------------------------------
# with_retry
# ---------------------------------------------------------------------------


class TestWithRetry:
    async def test_returns_on_first_success(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(return_value="ok")
        result = await with_retry(fn, state=state)
        assert result == "ok"
        assert fn.call_count == 1

    async def test_retries_on_429_then_succeeds(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(side_effect=[_http_error(429), _http_error(429), "ok"])
        with patch("alarina.github.rate_limit._backoff", new_callable=AsyncMock):
            result = await with_retry(fn, state=state, max_retries=3)
        assert result == "ok"
        assert fn.call_count == 3

    async def test_raises_after_max_retries_exhausted(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(side_effect=_http_error(429))
        with patch("alarina.github.rate_limit._backoff", new_callable=AsyncMock):
            with pytest.raises(httpx.HTTPStatusError):
                await with_retry(fn, state=state, max_retries=2)
        assert fn.call_count == 3  # initial + 2 retries

    async def test_does_not_retry_non_retryable_status(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(side_effect=_http_error(404))
        with pytest.raises(httpx.HTTPStatusError):
            await with_retry(fn, state=state, max_retries=3)
        assert fn.call_count == 1

    async def test_retries_transport_error(self) -> None:
        state = RateLimitState()
        connect_error = httpx.ConnectError("connection refused")
        fn = AsyncMock(side_effect=[connect_error, "ok"])
        with patch("alarina.github.rate_limit._backoff", new_callable=AsyncMock):
            result = await with_retry(fn, state=state, max_retries=2)
        assert result == "ok"

    async def test_passes_retry_after_header_to_backoff(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(side_effect=[_http_error(429, {"Retry-After": "60"}), "ok"])
        with patch("alarina.github.rate_limit._backoff", new_callable=AsyncMock) as mock_backoff:
            await with_retry(fn, state=state, max_retries=2)
        mock_backoff.assert_awaited_once_with(0, "60")

    async def test_calls_wait_if_exhausted_before_each_attempt(self) -> None:
        state = RateLimitState()
        fn = AsyncMock(side_effect=[_http_error(500), "ok"])
        with (
            patch("alarina.github.rate_limit._backoff", new_callable=AsyncMock),
            patch.object(state, "wait_if_exhausted", new_callable=AsyncMock) as mock_wait,
        ):
            await with_retry(fn, state=state, max_retries=2)
        assert mock_wait.call_count == 2  # once per attempt
