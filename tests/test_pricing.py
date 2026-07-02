"""Unit tests for model pricing logic."""

import pytest

from gateway.trace.pricing import estimate_cost


class TestPricing:
    """费用估算逻辑测试"""

    def test_known_model_gpt4o(self) -> None:
        """gpt-4o: input=1000, output=500 → 正确费用"""
        cost = estimate_cost("gpt-4o", 1000, 500)
        expected = 1000 * 2.50 / 1e6 + 500 * 10.00 / 1e6
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_known_model_gpt4o_mini(self) -> None:
        """gpt-4o-mini 正确计费"""
        cost = estimate_cost("gpt-4o-mini", 2000, 1000)
        expected = 2000 * 0.15 / 1e6 + 1000 * 0.60 / 1e6
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_known_model_claude_opus(self) -> None:
        """claude-3-opus 正确计费"""
        cost = estimate_cost("claude-3-opus-20240229", 500, 200)
        expected = 500 * 15.00 / 1e6 + 200 * 75.00 / 1e6
        assert cost == pytest.approx(expected, rel=1e-9)

    def test_unknown_model_returns_zero(self) -> None:
        """未知模型返回 0.0 不抛异常"""
        cost = estimate_cost("unknown-model-xyz", 100, 50)
        assert cost == 0.0

    def test_zero_tokens_returns_zero(self) -> None:
        """token=0 时费用为 0.0"""
        cost = estimate_cost("gpt-4o", 0, 0)
        assert cost == 0.0

    def test_large_token_count(self) -> None:
        """大 token 数不溢出"""
        cost = estimate_cost("gpt-4o", 1_000_000, 100_000)
        assert cost > 0
        assert isinstance(cost, float)

    def test_claude_haiku(self) -> None:
        """claude-3-haiku 正确计费"""
        cost = estimate_cost("claude-3-haiku-20240307", 800, 400)
        expected = 800 * 0.25 / 1e6 + 400 * 1.25 / 1e6
        assert cost == pytest.approx(expected, rel=1e-9)
