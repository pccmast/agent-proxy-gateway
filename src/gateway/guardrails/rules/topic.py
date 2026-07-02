"""Topic restriction rule — 限制 Agent 只回答特定领域的问题."""

import re
from typing import TYPE_CHECKING

from shared.models import GuardAction, GuardResult

from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState

# 默认的越界问题模式（非允许主题的通用检测）
_DEFAULT_DENY_PATTERNS: list[str] = [
    r"how\s+to\s+(build|make|create|hack)\b",
    r"write\s+(me\s+)?(a|an)\s+(virus|malware|exploit|ransomware)",
    r"(generate|create)\s+(fake|false|misleading)",
    r"帮我(写|做|生成|制作)",
]


class TopicRestrictionRule(BaseGuardRule):
    """限定 Agent 只回答允许主题范围内的问题。

    使用关键词启发式判断，不依赖外部分类模型。
    """

    rule_type: str = "topic_restriction"
    rule_id: str = "topic-restriction"
    action: GuardAction = GuardAction.BLOCK

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        allowed = self._config.get("allowed_topics", [])
        self._allowed_topics: list[str] = [str(t) for t in allowed] if isinstance(allowed, list) else []
        # 为每个 allowed_topic 编译关键词模式
        self._topic_patterns: dict[str, re.Pattern[str]] = {}
        for topic in self._allowed_topics:
            self._topic_patterns[topic] = re.compile(re.escape(topic), re.IGNORECASE)
        # 越界模式
        self._deny_patterns: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in _DEFAULT_DENY_PATTERNS]

    async def check_input(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="input")

    async def check_output(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text or not self._allowed_topics:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        text_lower = text.lower()

        # 检查是否匹配任一允许主题
        for topic, pattern in self._topic_patterns.items():
            if pattern.search(text_lower):
                return GuardResult(rule_id=self.rule_id, action=self.action)

        # 检查是否匹配越界模式
        deny_matches: list[str] = []
        for pattern in self._deny_patterns:
            for m in pattern.finditer(text_lower):
                deny_matches.append(m.group())

        confidence = min(len(deny_matches) * 0.3, 0.8) if deny_matches else 0.0
        return GuardResult(
            rule_id=self.rule_id,
            action=self.action if confidence >= self.confidence_threshold else GuardAction.LOG,
            matches=deny_matches,
            confidence=confidence,
            details=f"[{phase}] topic out of allowed scope: {', '.join(self._allowed_topics)}",
        )
