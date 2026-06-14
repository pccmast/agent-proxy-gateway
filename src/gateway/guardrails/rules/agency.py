"""Excessive agency + tool call loop detection rules."""

import json
import re
from typing import TYPE_CHECKING

from shared.models import GuardResult, GuardAction
from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState


# ============================================================================
# ExcessiveAgencyRule — 输出侧：限制 Agent 的工具调用范围
# ============================================================================

class ExcessiveAgencyRule(BaseGuardRule):
    """限制 Agent 的工具调用范围，防止越权操作（OWASP LLM06）。

    检测：工具调用白名单/黑名单、参数级安全检查。
    """

    rule_type: str = "excessive_agency"
    rule_id: str = "excessive-agency"
    action: GuardAction = GuardAction.BLOCK

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        allowed = self._config.get("allowed_tools", [])
        self._allowed_tools: set[str] = set(str(t) for t in allowed) if isinstance(allowed, list) else set()
        denied = self._config.get("denied_tools", [])
        self._denied_tools: set[str] = set(str(t) for t in denied) if isinstance(denied, list) else set()

        # 参数级危险模式
        param_patterns = self._config.get("parameter_deny_patterns", [])
        self._param_rules: list[tuple[str, list[str]]] = []
        if isinstance(param_patterns, list):
            for pp in param_patterns:
                if isinstance(pp, dict):
                    field = str(pp.get("field", ""))
                    denies = pp.get("deny", [])
                    if field and isinstance(denies, list):
                        self._param_rules.append((field, [str(d) for d in denies]))

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

        tool_calls = self._extract_tool_calls(text)
        if not tool_calls:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matched: list[str] = []
        max_conf = 0.0

        for tc in tool_calls:
            tool_name = tc.get("name", "")
            # 黑名单检查
            if tool_name in self._denied_tools:
                matched.append(f"denied_tool:{tool_name}")
                max_conf = 1.0
                continue
            # 白名单检查
            if self._allowed_tools and tool_name not in self._allowed_tools and "*" not in self._allowed_tools:
                matched.append(f"not_allowed:{tool_name}")
                max_conf = max(max_conf, 0.9)
            # 参数检查
            args = tc.get("arguments", {})
            if isinstance(args, dict):
                for field, deny_values in self._param_rules:
                    value = str(args.get(field, "")).lower()
                    for deny_val in deny_values:
                        if deny_val.lower() in value:
                            matched.append(f"dangerous_param:{field}={value[:50]}")
                            max_conf = max(max_conf, 0.95)

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action if matched else GuardAction.LOG,
            matches=matched,
            confidence=max_conf,
            details=f"[{phase}] {len(matched)} agency violation(s)",
        )

    @staticmethod
    def _extract_tool_calls(text: str) -> list[dict[str, object]]:
        """从文本中提取 tool_calls JSON."""
        results: list[dict[str, object]] = []
        # 尝试匹配 JSON 中的 tool_calls
        tool_calls_pattern = re.compile(r'"tool_calls"\s*:\s*(\[.*?\])', re.DOTALL)
        for m in tool_calls_pattern.finditer(text):
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict) and "function" in item:
                            func = item["function"]
                            results.append({
                                "name": func.get("name", ""),
                                "arguments": func.get("arguments", {}),
                            })
            except (json.JSONDecodeError, TypeError):
                continue

        # 尝试匹配单个 tool 名称 + 参数
        single_pattern = re.compile(r'"(?:name|tool)"\s*:\s*"([^"]+)"', re.IGNORECASE)
        for m in single_pattern.finditer(text):
            results.append({"name": m.group(1), "arguments": {}})

        return results


# ============================================================================
# ToolCallLoopRule — 输出侧：检测工具调用死循环
# ============================================================================

class ToolCallLoopRule(BaseGuardRule):
    """检测 Agent 是否陷入工具调用死循环（OWASP 资源滥用）。

    依赖 SessionState 追踪跨请求工具调用模式。
    """

    rule_type: str = "tool_call_loop"
    rule_id: str = "tool-call-loop"
    action: GuardAction = GuardAction.BLOCK

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._max_consecutive: int = int(self._config.get("max_consecutive_same_tool", 3))
        self._max_cycle_length: int = int(self._config.get("max_cycle_length", 4))
        self._max_total_calls: int = int(self._config.get("max_total_tool_calls", 50))

    async def check_input(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return GuardResult(rule_id=self.rule_id, action=self.action)

    async def check_output(
        self, text: str, session: "SessionState | None" = None
    ) -> GuardResult:
        return self._check(text, session, phase="output")

    def _check(
        self, text: str, session: "SessionState | None", phase: str
    ) -> GuardResult:
        if session is None:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        tool_calls = ExcessiveAgencyRule._extract_tool_calls(text)
        if not tool_calls:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matched: list[str] = []
        max_conf = 0.0

        # 更新 session 中的工具调用历史
        for tc in tool_calls:
            tool_name = tc.get("name", "")

            # 检测连续同一工具调用 — 和历史最后一条比较（追加之前）
            if session.tool_call_history:
                last_name = session.tool_call_history[-1].get("name", "")
                if tool_name == last_name:
                    session.consecutive_same_tool += 1
                else:
                    session.consecutive_same_tool = 1
            else:
                session.consecutive_same_tool = 1

            # 追加当前调用到历史（在检测之后）
            session.tool_call_history.append(
                {"name": tool_name, "arguments": tc.get("arguments", {})}
            )
            session.total_tool_calls += 1

            if session.consecutive_same_tool >= self._max_consecutive:
                matched.append(f"consecutive:{tool_name}x{session.consecutive_same_tool}")
                max_conf = max(max_conf, 0.9)

        # 检测总量超限
        if session.total_tool_calls >= self._max_total_calls:
            matched.append(f"total_exceeded:{session.total_tool_calls}")
            max_conf = max(max_conf, 0.95)

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action if matched else GuardAction.LOG,
            matches=matched,
            confidence=max_conf,
            details=f"[{phase}] {len(matched)} loop violation(s)",
        )
