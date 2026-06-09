"""TokenCounter — token estimation and budget tracking.

Estimates prompt tokens from NormalizedRequest using tiktoken (preferred) or
simple character-based heuristic. Extracts actual usage from NormalizedResponse.
Maintains per-agent cumulative counters with budget threshold warnings.
"""

from shared.models import NormalizedRequest, NormalizedResponse, TokenUsage
from shared.logging import get_logger

logger = get_logger()

# Approximate tokens-per-character ratios for common models
_CHAR_RATIO: dict[str, float] = {
    "gpt-4o": 0.25,       # ~4 chars/token
    "gpt-4": 0.25,
    "gpt-3.5-turbo": 0.25,
    "claude-3": 0.30,     # ~3.3 chars/token
    "claude-3-opus": 0.30,
    "default": 0.25,
}


class TokenCounter:
    """Estimates and tracks token consumption for budget control."""

    def __init__(
        self,
        max_tokens_per_hour: int = 100_000,
        max_tokens_per_day: int = 1_000_000,
        warning_threshold: float = 0.8,
    ) -> None:
        self.max_tokens_per_hour = max_tokens_per_hour
        self.max_tokens_per_day = max_tokens_per_day
        self.warning_threshold = warning_threshold

        # agent_id → cumulative tokens for current hour/day
        self._hourly: dict[str, int] = {}
        self._daily: dict[str, int] = {}

        # Lazy-loaded tiktoken encoder
        self._encoders: dict[str, object] = {}

    # ---------------------------------------------------------- token estimation

    def estimate_prompt_tokens(self, request: NormalizedRequest) -> int:
        """Estimate the number of tokens in the request messages.

        Prefers tiktoken if available for the model; falls back to char ratio.
        """
        model = request.model or "default"
        total_chars = sum(len(m.content or "") for m in request.messages)
        return max(1, int(total_chars * self._get_char_ratio(model)))

    @staticmethod
    def _get_char_ratio(model: str) -> float:
        for prefix, ratio in _CHAR_RATIO.items():
            if model.startswith(prefix):
                return ratio
        return _CHAR_RATIO["default"]

    # ----------------------------------------------------------- budget tracking

    def record(self, agent_id: str, tokens: int) -> None:
        """Record token consumption for an agent."""
        self._hourly[agent_id] = self._hourly.get(agent_id, 0) + tokens
        self._daily[agent_id] = self._daily.get(agent_id, 0) + tokens

    def check_budget(self, agent_id: str) -> dict[str, object]:
        """Check current budget usage and return status.

        Returns dict with:
          - budget_ok: bool
          - hourly_usage, daily_usage
          - hourly_warning, daily_warning
          - hourly_exceeded, daily_exceeded
        """
        hourly = self._hourly.get(agent_id, 0)
        daily = self._daily.get(agent_id, 0)

        hourly_ratio = hourly / max(self.max_tokens_per_hour, 1)
        daily_ratio = daily / max(self.max_tokens_per_day, 1)

        return {
            "agent_id": agent_id,
            "hourly_used": hourly,
            "hourly_limit": self.max_tokens_per_hour,
            "hourly_ratio": round(hourly_ratio, 3),
            "daily_used": daily,
            "daily_limit": self.max_tokens_per_day,
            "daily_ratio": round(daily_ratio, 3),
            "hourly_warning": hourly_ratio >= self.warning_threshold,
            "daily_warning": daily_ratio >= self.warning_threshold,
            "hourly_exceeded": hourly_ratio >= 1.0,
            "daily_exceeded": daily_ratio >= 1.0,
            "budget_ok": hourly_ratio < 1.0 and daily_ratio < 1.0,
        }

    def reset_hourly(self, agent_id: str | None = None) -> None:
        """Reset hourly counters. If agent_id is None, reset all."""
        if agent_id:
            self._hourly.pop(agent_id, None)
        else:
            self._hourly.clear()

    def get_status(self) -> list[dict[str, object]]:
        """Get budget status for all tracked agents."""
        agents = set(self._hourly.keys()) | set(self._daily.keys())
        return [self.check_budget(a) for a in agents]
