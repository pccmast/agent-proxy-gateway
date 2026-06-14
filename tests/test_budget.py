"""Tests for budget / rate limiter / circuit breaker."""

import time
import pytest

from gateway.budget.rate_limiter import SlidingWindowRateLimiter, RateLimitConfig
from gateway.budget.token_counter import TokenCounter
from gateway.budget.circuit_breaker import CircuitBreaker, CircuitState
from gateway.proxy.middleware import BlockException, RateLimitException
from shared.models import (
    RequestContext, ResponseContext, NormalizedRequest, NormalizedResponse, Message, TokenUsage,
)


# ==========================================================================
# Rate Limiter
# ==========================================================================

class TestSlidingWindowRateLimiter:
    @pytest.fixture
    def limiter(self):
        return SlidingWindowRateLimiter(default_rpm=5, default_tpm=1000)

    @pytest.mark.asyncio
    async def test_under_limit(self, limiter):
        ctx = RequestContext(
            trace_id="t", span_id="s",
            request=NormalizedRequest(provider="openai", model="gpt-4o", messages=[Message(role="user", content="hi")]),
        )
        result = await limiter.on_request(ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_exceeded_rpm(self, limiter):
        """Send 6 requests when limit is 5 → 6th should be blocked."""
        ctx = RequestContext(
            trace_id="t", span_id="s",
            request=NormalizedRequest(provider="openai", model="gpt-4o", messages=[Message(role="user", content="hi")]),
            headers={"X-Agent-ID": "test-agent"},
        )
        for _ in range(5):
            await limiter.on_request(ctx)
        with pytest.raises(RateLimitException) as exc:
            await limiter.on_request(ctx)
        assert exc.value.rule_id == "rate-limiter"
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_response_records_tokens(self, limiter):
        ctx = ResponseContext(
            trace_id="t", span_id="s",
            request=NormalizedRequest(provider="openai", model="gpt-4o", messages=[]),
            response=NormalizedResponse(
                provider="openai", model="gpt-4o", content="ok",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            ),
        )
        result = await limiter.on_response(ctx)
        assert result is not None

    def test_get_status(self, limiter):
        status = limiter.get_status()
        assert isinstance(status, dict)


# ==========================================================================
# Token Counter
# ==========================================================================

class TestTokenCounter:
    @pytest.fixture
    def counter(self):
        return TokenCounter(max_tokens_per_hour=1000, max_tokens_per_day=10000)

    def test_estimate_tokens(self, counter):
        req = NormalizedRequest(
            provider="openai", model="gpt-4o",
            messages=[Message(role="user", content="Hello, how are you?")],
        )
        tokens = counter.estimate_prompt_tokens(req)
        assert tokens > 0

    def test_record_and_check(self, counter):
        counter.record("agent-1", 500)
        status = counter.check_budget("agent-1")
        assert status["hourly_used"] == 500
        assert status["budget_ok"] is True

    def test_budget_exceeded(self, counter):
        counter.record("agent-1", 1100)
        status = counter.check_budget("agent-1")
        assert status["hourly_exceeded"] is True
        assert status["budget_ok"] is False

    def test_warning_threshold(self, counter):
        counter.record("agent-1", 850)  # 85% of 1000 → > 80%
        status = counter.check_budget("agent-1")
        assert status["hourly_warning"] is True

    def test_get_status(self, counter):
        counter.record("agent-1", 100)
        counter.record("agent-2", 200)
        statuses = counter.get_status()
        assert len(statuses) == 2

    def test_reset_hourly(self, counter):
        counter.record("agent-1", 500)
        counter.reset_hourly("agent-1")
        status = counter.check_budget("agent-1")
        assert status["hourly_used"] == 0


# ==========================================================================
# Circuit Breaker
# ==========================================================================

class TestCircuitBreaker:
    @pytest.fixture
    def cb(self):
        return CircuitBreaker(failure_threshold=3, recovery_timeout=0.1, half_open_max_calls=1)

    def test_initial_closed(self, cb):
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_transitions_to_open(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_transitions_to_half_open(self, cb):
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.allow_request() is True  # half_open allows 1 probe

    def test_half_open_success_closes(self, cb):
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.allow_request() is True
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens(self, cb):
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.15)
        assert cb.allow_request() is True
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_reset(self, cb):
        for _ in range(3):
            cb.record_failure()
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    def test_get_status(self, cb):
        status = cb.get_status()
        assert status["state"] == "closed"
