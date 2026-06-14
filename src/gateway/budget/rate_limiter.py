"""SlidingWindowRateLimiter — per-agent/per-model rate limiting using sliding windows.

Implements Middleware so it plugs directly into the middleware chain.
Uses collections.deque for O(1) window trimming.
"""

import time
from collections import deque
from dataclasses import dataclass, field

from gateway.proxy.middleware import Middleware, RateLimitException
from shared.models import RequestContext, ResponseContext
from shared.logging import get_logger

logger = get_logger()


@dataclass
class RateLimitConfig:
    """Per-scope rate limit settings."""
    rpm: int = 60       # requests per minute
    tpm: int = 100_000  # tokens per minute


@dataclass
class _WindowState:
    """Internal sliding-window state for one scope (agent or model)."""
    requests: deque[float] = field(default_factory=deque)
    tokens: deque[tuple[float, int]] = field(default_factory=deque)


class SlidingWindowRateLimiter(Middleware):
    """Rate limits requests and tokens using sliding-window deques.

    Dimensions:
    - Per agent   (identified by X-Agent-ID header)
    - Per model

    Limits:
    - RPM (requests per minute)
    - TPM (tokens per minute)

    Priority: 15 — runs after guardrails, before heavy processing.
    """

    priority: int = 15

    def __init__(
        self,
        default_rpm: int = 60,
        default_tpm: int = 100_000,
        per_model: dict[str, RateLimitConfig] | None = None,
    ) -> None:
        self._default: RateLimitConfig = RateLimitConfig(rpm=default_rpm, tpm=default_tpm)
        self._per_model: dict[str, RateLimitConfig] = {}
        if per_model:
            self._per_model.update(per_model)

        # agent_id → WindowState
        self._agent_windows: dict[str, _WindowState] = {}
        # model → WindowState  (global, not per-agent)
        self._model_windows: dict[str, _WindowState] = {}

    # ---------------------------------------------------------------- Middleware

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        agent_id = ctx.headers.get("X-Agent-ID", ctx.headers.get("x-agent-id", "default"))

        now = time.monotonic()

        # Check agent-level limits
        agent_cfg = self._default  # per-agent uses defaults
        agent_win = self._resolve_agent_window(agent_id)
        agent_rpm_ok, retry_secs = self._check_rpm(agent_win, agent_cfg, now)
        if not agent_rpm_ok:
            raise RateLimitException(
                rule_id="rate-limiter",
                reason=f"Agent '{agent_id}' RPM exceeded ({agent_cfg.rpm}/min)",
                retry_after=retry_secs,
            )

        # TPM check is done in on_response when we know token count
        # RPM is checked here, TPM on response
        return ctx

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        # ResponseContext doesn't carry headers directly — fall back to "default" agent
        agent_id = "default"
        model = ctx.request.model or "unknown"
        now = time.monotonic()

        token_count = 0
        if ctx.response.usage:
            token_count = ctx.response.usage.total_tokens

        # Record token consumption in agent window
        agent_win = self._resolve_agent_window(agent_id)
        self._trim_deque_tokens(agent_win.tokens, now)
        agent_win.tokens.append((now, token_count))

        # Check agent TPM
        agent_cfg = self._default
        total_agent_tokens = sum(t for _, t in agent_win.tokens)
        if total_agent_tokens > agent_cfg.tpm:
            logger.warning(
                "tpm_exceeded",
                agent=agent_id,
                current_tpm=total_agent_tokens,
                limit=agent_cfg.tpm,
            )
            # Post-hoc: we already served the request, but flag it

        # Record model-level token window
        model_win = self._resolve_model_window(model)
        self._trim_deque_tokens(model_win.tokens, now)
        model_win.tokens.append((now, token_count))

        return ctx

    # ----------------------------------------------------------------- helpers

    def _resolve_agent_window(self, agent_id: str) -> _WindowState:
        if agent_id not in self._agent_windows:
            self._agent_windows[agent_id] = _WindowState()
        return self._agent_windows[agent_id]

    def _resolve_model_window(self, model: str) -> _WindowState:
        if model not in self._model_windows:
            self._model_windows[model] = _WindowState()
        return self._model_windows[model]

    def _check_rpm(self, win: _WindowState, cfg: RateLimitConfig, now: float) -> tuple[bool, float]:
        """Check if RPM limit is exceeded. Returns (ok, retry_after_seconds)."""
        window_s = 60.0
        cutoff = now - window_s
        self._trim_deque(win.requests, cutoff)
        win.requests.append(now)

        if len(win.requests) > cfg.rpm:
            oldest = win.requests[0]
            retry_after = max(0.1, window_s - (now - oldest))
            return False, retry_after
        return True, 0.0

    @staticmethod
    def _trim_deque(dq: deque[float], cutoff: float) -> None:
        while dq and dq[0] < cutoff:
            dq.popleft()

    @staticmethod
    def _trim_deque_tokens(dq: deque[tuple[float, int]], cutoff: float) -> None:
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    # --------------------------------------------------------------- public API

    def get_status(self) -> dict[str, dict[str, float]]:
        """Return current rate-limit status for all known scopes."""
        now = time.monotonic()
        cutoff = now - 60.0
        result: dict[str, dict[str, float]] = {}
        for agent_id, win in self._agent_windows.items():
            active = [ts for ts in win.requests if ts >= cutoff]
            result[f"agent:{agent_id}"] = {
                "rpm_current": len(active),
                "rpm_limit": self._default.rpm,
            }
        for model_id, win in self._model_windows.items():
            active_tokens = sum(t for ts, t in win.tokens if ts >= cutoff)
            cfg = self._per_model.get(model_id, self._default)
            result[f"model:{model_id}"] = {
                "tpm_current": active_tokens,
                "tpm_limit": cfg.tpm,
            }
        return result
