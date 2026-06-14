"""Output format validation rule — 验证模型输出格式是否符合预期."""

import json
from typing import TYPE_CHECKING

from shared.models import GuardResult, GuardAction
from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState


class OutputFormatValidationRule(BaseGuardRule):
    """验证模型输出格式是否符合期望（JSON Schema / text）。

    action 默认为 "log"（初期只记录，不拦截，因为误报率高）。
    """

    rule_type: str = "output_format"
    rule_id: str = "output-format-validation"
    action: GuardAction = GuardAction.LOG

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._expected_format: str = str(self._config.get("expected_format", "text"))
        raw_schema = self._config.get("json_schema")
        self._json_schema: dict[str, object] | None = raw_schema if isinstance(raw_schema, dict) else None
        self._on_mismatch: str = str(self._config.get("on_mismatch", "log"))

    async def check_input(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    async def check_output(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return self._check(text, phase="output")

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        errors: list[str] = []

        if self._expected_format == "json":
            # 尝试解析为 JSON
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                errors.append(f"not_valid_json: {e}")
                return GuardResult(
                    rule_id=self.rule_id,
                    action=GuardAction(self._on_mismatch),
                    matches=errors,
                    confidence=0.9,
                    details=f"[{phase}] output is not valid JSON",
                )

            # 如果有 JSON Schema，做简单校验
            if self._json_schema and isinstance(parsed, dict):
                required = self._json_schema.get("required", [])
                if isinstance(required, list):
                    missing = [k for k in required if k not in parsed]
                    if missing:
                        errors.append(f"missing_fields: {missing}")

            if errors:
                return GuardResult(
                    rule_id=self.rule_id,
                    action=GuardAction(self._on_mismatch),
                    matches=errors,
                    confidence=0.7,
                    details=f"[{phase}] JSON schema validation failed",
                )

        return GuardResult(rule_id=self.rule_id, action=self.action)
