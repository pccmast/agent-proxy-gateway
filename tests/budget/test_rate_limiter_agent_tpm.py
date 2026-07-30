"""Tests for per-agent TPM accounting (Bug1 fix).

Before the fix, RateLimiter.on_response() hardcoded agent_id="default",
so per-agent TPM tokens always landed in the "default" bucket regardless of
the X-Agent-ID header. After the fix, on_response reads ResponseContext.agent_id.
"""

import pytest

from gateway.budget.rate_limiter import SlidingWindowRateLimiter
from shared.models import (
    NormalizedRequest,
    NormalizedResponse,
    ResponseContext,
    TokenUsage,
)


def _resp(agent_id: str, total: int) -> ResponseContext:
    return ResponseContext(
        trace_id="t",
        span_id="s",
        agent_id=agent_id,
        request=NormalizedRequest(provider="openai", model="gpt-4o", messages=[]),
        response=NormalizedResponse(
            provider="openai",
            model="gpt-4o",
            content="ok",
            usage=TokenUsage(prompt_tokens=total, completion_tokens=0, total_tokens=total),
        ),
    )


@pytest.mark.asyncio
async def test_tpm_keyed_per_agent():
    limiter = SlidingWindowRateLimiter(default_rpm=5, default_tpm=100_000)

    await limiter.on_response(_resp("agent-a", 15))
    await limiter.on_response(_resp("agent-b", 25))

    # Each agent has its own token window.
    assert "agent-a" in limiter._agent_windows
    assert "agent-b" in limiter._agent_windows
    assert sum(t for _, t in limiter._agent_windows["agent-a"].tokens) == 15
    assert sum(t for _, t in limiter._agent_windows["agent-b"].tokens) == 25

    # The broken "default" bucket must NOT absorb these tokens.
    assert "default" not in limiter._agent_windows


@pytest.mark.asyncio
async def test_missing_agent_id_falls_back_to_default():
    limiter = SlidingWindowRateLimiter()
    await limiter.on_response(_resp("", 10))
    assert "default" in limiter._agent_windows
    assert sum(t for _, t in limiter._agent_windows["default"].tokens) == 10
