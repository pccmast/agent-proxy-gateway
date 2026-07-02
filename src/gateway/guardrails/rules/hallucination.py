"""Hallucination indicator rule — 轻量级幻觉信号检测."""

import re
from typing import TYPE_CHECKING

from shared.models import GuardAction, GuardResult

from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState

_DEFAULT_CITATION_PATTERNS: list[str] = [
    r"(?:根据|according to)\s.{2,30}(?:et\s+al\.|等人).*(?:20\d{2}|19\d{2})",
    r"(?:研究表明|studies\s+show).*(?:\d+%|百分之\d+)",
    r"(?:据|according\s+to)\s*(?:报道|report)",
]


class HallucinationIndicatorRule(BaseGuardRule):
    """轻量级幻觉信号检测。

    action 固定为 "log"（只记录，不拦截）—— 误报率太高。
    检测：虚构引用、可疑 URL。
    """

    rule_type: str = "hallucination_indicator"
    rule_id: str = "hallucination-indicator"
    action: GuardAction = GuardAction.LOG

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        patterns = self._config.get("citation_patterns", [])
        if isinstance(patterns, list) and patterns:
            raw = [str(p) for p in patterns]
        else:
            raw = _DEFAULT_CITATION_PATTERNS
        self._patterns: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in raw]

    async def check_input(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    async def check_output(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="output")

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matched: list[str] = []

        # 检测引用模式
        for pattern in self._patterns:
            for m in pattern.finditer(text):
                matched.append(m.group())

        confidence = min(len(matched) * 0.2, 0.6) if matched else 0.0
        return GuardResult(
            rule_id=self.rule_id,
            action=self.action,
            matches=matched,
            confidence=confidence,
            details=f"[{phase}] {len(matched)} hallucination indicator(s) found",
        )
