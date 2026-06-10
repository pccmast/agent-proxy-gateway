"""Multi-turn jailbreak detection rule — 渐进式越狱检测.

使用加权累计评分追踪跨多轮的渐进式越狱攻击。
依赖 SessionStore 维护 per-session 的 escalation 状态。
"""

import re
from typing import TYPE_CHECKING

from shared.models import GuardResult, GuardAction
from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState

# 默认的三类渐进式攻击信号
_DEFAULT_ESCALATION_SIGNALS: list[dict[str, object]] = [
    {
        "type": "boundary_testing",
        "description": "用户在试探模型的拒绝边界",
        "patterns": ["你能...吗", "如果是假设性的呢", "从学术角度", "can you tell me"],
        "weight": 0.3,
    },
    {
        "type": "role_negotiation",
        "description": "用户试图重新定义模型的角色",
        "patterns": ["你现在扮演", "假装你是", "进入开发者模式", "pretend you are", "act as if"],
        "weight": 0.4,
    },
    {
        "type": "context_manipulation",
        "description": "用户通过虚构上下文绕过限制",
        "patterns": ["这是小说情节", "用于安全教育", "这是授权测试", "for educational purposes", "this is a test"],
        "weight": 0.35,
    },
]


class MultiTurnJailbreakRule(BaseGuardRule):
    """多轮渐进式越狱检测。

    每一轮命中 escalation_signals 时累加权重到 session.escalation_score，
    超过 escalation_threshold 时触发 block 并重置 session。
    """

    rule_type: str = "multi_turn_jailbreak"
    rule_id: str = "multi-turn-jailbreak"
    action: GuardAction = GuardAction.BLOCK

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        signals = self._config.get("escalation_signals", [])
        if isinstance(signals, list) and signals:
            self._signals = signals
        else:
            self._signals = _DEFAULT_ESCALATION_SIGNALS
        self._threshold: float = float(
            self._config.get("escalation_threshold", 0.8)
        )
        self._max_history: int = int(self._config.get("max_history_turns", 20))

        # 预编译每个 signal 的 patterns
        self._compiled_signals: list[dict[str, object]] = []
        for sig in self._signals:
            patterns = sig.get("patterns", [])
            if isinstance(patterns, list):
                compiled_patterns = [
                    re.compile(str(p), re.IGNORECASE) for p in patterns
                ]
            else:
                compiled_patterns = []
            self._compiled_signals.append({
                "type": sig.get("type", ""),
                "weight": float(sig.get("weight", 0.3)),
                "patterns": compiled_patterns,
            })

    async def check_input(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return self._check(text, session, phase="input")

    async def check_output(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    def _check(
        self, text: str, session: "SessionState | None", phase: str
    ) -> GuardResult:
        if not text or session is None:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        # 裁剪历史
        if len(session.history) > self._max_history:
            session.history = session.history[-self._max_history:]

        # 累加本轮命中信号的权重
        text_lower = text.lower()
        matched_signals: list[str] = []
        total_weight = 0.0

        for sig in self._compiled_signals:
            patterns = sig.get("patterns", [])
            if not isinstance(patterns, list):
                continue
            for pattern in patterns:
                if isinstance(pattern, re.Pattern) and pattern.search(text_lower):
                    matched_signals.append(str(sig.get("type", "")))
                    total_weight += float(sig.get("weight", 0.3))
                    break  # 每个 signal 类型只计数一次

        session.escalation_score += total_weight

        # 记录本轮历史
        session.history.append({"role": "user", "content": text[:200]})

        triggered = session.escalation_score >= self._threshold

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action if triggered else GuardAction.LOG,
            matches=matched_signals,
            confidence=min(session.escalation_score, 1.0),
            details=(
                f"[{phase}] escalation_score={session.escalation_score:.2f}"
                + (f" — THRESHOLD EXCEEDED" if triggered else "")
            ),
        )
