"""Behavior anomaly detection rule — 基线偏离检测."""

from typing import TYPE_CHECKING

from shared.models import GuardAction, GuardResult

from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState


class BehaviorAnomalyRule(BaseGuardRule):
    """行为异常检测 — 基于硬编码基线的偏离检测。

    MVP 阶段使用配置中的 baseline 值，未来可从 TraceStore 自动学习。
    action 固定为 "log"（只记录，不拦截）。
    """

    rule_type: str = "anomaly_detection"
    rule_id: str = "behavior-anomaly"
    action: GuardAction = GuardAction.LOG

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        baselines = self._config.get("baselines", {})
        if isinstance(baselines, dict):
            self._prompt_mean: float = float(baselines.get("prompt_length_mean", 350))
            self._prompt_std: float = float(baselines.get("prompt_length_std", 200))
        else:
            self._prompt_mean = 350.0
            self._prompt_std = 200.0
        self._alert_sigma: float = float(self._config.get("alert_at_sigma", 3.0))

    async def check_input(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="input")

    async def check_output(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matched: list[str] = []
        confidence = 0.0

        # 检查 prompt 长度是否偏离基线
        text_len = len(text)
        if self._prompt_std > 0:
            deviation = abs(text_len - self._prompt_mean) / self._prompt_std
            if deviation > self._alert_sigma:
                matched.append(f"prompt_length_anomaly: {text_len} (mean={self._prompt_mean}, sigma={deviation:.1f})")
                confidence = min(deviation / (self._alert_sigma * 2), 0.9)

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action,
            matches=matched,
            confidence=confidence,
            details=f"[{phase}] anomaly detection: {len(matched)} signal(s)",
        )
