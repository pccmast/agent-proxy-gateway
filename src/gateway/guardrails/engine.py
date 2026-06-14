"""GuardrailsEngine — main middleware that orchestrates all guardrail rules.

v2 — 四层 AI Safety Platform 引擎:
- input_rules: 输入阶段规则
- output_rules: 输出阶段规则
- behavioral_rules: 行为阶段规则（需要 session）
- audit: 审计记录

插件式发现 + phase 分组调度 + scope 匹配 + session 传递。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from gateway.proxy.middleware import Middleware, BlockException
from shared.models import (
    GuardResult,
    GuardAction,
    RequestContext,
    ResponseContext,
    StreamChunk,
    StreamContext,
)
from shared.logging import get_logger

from .rules.base import BaseGuardRule
from .action import apply_redact_to_messages, format_block_reason
from .scope import ScopeMatcher
from .config import RuleScope, SessionState, AuditEvent

if TYPE_CHECKING:
    from .session import SessionStore
    from .audit import AuditLogger

logger = get_logger()

# ── 按 phase 分类的 rule_type 列表 ──
# 当 discover_rules 找不到某类型时，这些值作为 fallback
INPUT_RULE_TYPES = {
    "injection", "pii", "content", "content_safety",
    "system_prompt_extraction", "topic_restriction",
}
OUTPUT_RULE_TYPES = {
    "system_prompt_leakage", "excessive_agency",
    "output_format", "hallucination_indicator",
}
BEHAVIORAL_RULE_TYPES = {
    "multi_turn_jailbreak", "tool_call_loop",
    "anomaly_detection",
}


class GuardrailsEngine(Middleware):
    """AI Safety Platform 引擎 — v2 升级版。

    Request phase  (priority=10): input_rules + behavioral_rules (input)
    Response phase (priority=10): output_rules + behavioral_rules (output)
    Stream phase:                 input_rules on accumulated content
    """

    priority: int = 10

    def __init__(
        self,
        rule_configs: list[dict[str, object]] | None = None,
        session_store: "SessionStore | None" = None,
        audit_logger: "AuditLogger | None" = None,
    ) -> None:
        self._input_rules: list[BaseGuardRule] = []
        self._output_rules: list[BaseGuardRule] = []
        self._behavioral_rules: list[BaseGuardRule] = []
        self._hit_stats: dict[str, dict[str, int]] = {}  # rule_id → {"block":N, "redact":N, "log":N, "total":N}
        self._category_stats: dict[str, int] = {}         # category → hit count (violence/self_harm/illegal/hate)
        self._session_store = session_store
        self._audit_logger = audit_logger

        if rule_configs:
            self._load_rules(rule_configs)

    # ------------------------------------------------------------------ factory

    @classmethod
    def from_policy_store(cls, policy_store: object) -> "GuardrailsEngine":
        """Build a GuardrailsEngine from a PolicyStore (backward compat)."""
        from typing import cast
        from gateway.policy.store import PolicyStore
        config = cast(PolicyStore, policy_store).guardrails_config()
        rule_dicts = [r.model_dump() for r in config.rules]
        return cls(rule_configs=[cast(dict[str, object], rd) for rd in rule_dicts])

    # ------------------------------------------------------------------ 规则加载

    def _load_rules(
        self,
        rule_configs: list[dict[str, object]],
    ) -> None:
        """Instantiate rules from configuration dicts (v2 — plugin discovery).

        使用 discover_rules() 自动发现所有规则类型，
        不再依赖硬编码的 _RULE_FACTORY。
        """
        from typing import Any as AnyType

        # 插件发现
        registry = BaseGuardRule.discover_rules()

        self._input_rules.clear()
        self._output_rules.clear()
        self._behavioral_rules.clear()

        for cfg in rule_configs:
            if not cfg.get("enabled", True):
                continue

            rule_type = cast(str, cfg.get("type", ""))
            rule_cls = registry.get(rule_type)
            if rule_cls is None:
                logger.warning("unknown_rule_type", type=rule_type, id=cfg.get("id"))
                continue

            try:
                # 解析 scope
                scope_data = cfg.get("scope", {})
                if isinstance(scope_data, dict):
                    scope = RuleScope(
                        models=cast(list[str], scope_data.get("models", ["*"])),
                        agents=cast(list[str], scope_data.get("agents", ["*"])),
                    )
                else:
                    scope = RuleScope()

                # 解析 config 子对象
                rule_config = cfg.get("config", {})
                if not isinstance(rule_config, dict):
                    rule_config = {}

                rule = cast(AnyType, rule_cls)(  # pyright: ignore[reportArgumentType]
                    rule_id=cast(str, cfg.get("id", rule_type)),
                    action=cast(str, cfg.get("action", "log")),
                    severity=cast(str, cfg.get("severity", "medium")),
                    confidence_threshold=cast(float, cfg.get("confidence_threshold", 0.7)),
                    enabled=cast(bool, cfg.get("enabled", True)),
                    scope=scope,
                    config=cast(dict[str, object], rule_config),
                )

                # 按 phase 分组
                phase = cast(str, cfg.get("phase", "input"))
                if rule_type in BEHAVIORAL_RULE_TYPES:
                    self._behavioral_rules.append(rule)
                elif phase == "output" or rule_type in OUTPUT_RULE_TYPES:
                    self._output_rules.append(rule)
                else:
                    self._input_rules.append(rule)

                logger.debug("guard_rule_loaded", rule_id=rule.rule_id, type=rule_type, phase=phase)
            except Exception as e:
                logger.error("rule_init_error", rule_id=cfg.get("id"), error=str(e))

        logger.info(
            "guardrails_engine_v2_loaded",
            input_count=len(self._input_rules),
            output_count=len(self._output_rules),
            behavioral_count=len(self._behavioral_rules),
        )

    # ---------------------------------------------------------------- Middleware

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        """执行输入规则 + 行为规则（input 阶段）."""
        if not self._has_rules():
            return ctx

        # 清理过期 session
        if self._session_store:
            self._session_store.evict_expired()

        # 获取会话状态
        session = self._get_session(ctx)

        # 收集文本
        texts = [m.content for m in ctx.request.messages if m.content]
        full_text = "\n".join(texts)
        model = ctx.request.model if hasattr(ctx.request, "model") else ""
        agent_id = getattr(ctx, "provider", "default")

        # 执行 input_rules
        for rule in self._input_rules:
            if not rule.is_enabled():
                continue
            if rule.scope and not ScopeMatcher.matches(rule.scope, model, agent_id):
                continue
            result = await rule.check_input(full_text, session=session)
            await self._apply_guard_result(result, ctx, "input")

        # 执行 behavioral_rules (input 阶段)
        for rule in self._behavioral_rules:
            if not rule.is_enabled():
                continue
            if rule.scope and not ScopeMatcher.matches(rule.scope, model, agent_id):
                continue
            result = await rule.check_input(full_text, session=session)
            # 特殊处理：jailbreak 触发时重置 session
            if result.matches and result.rule_id == "multi-turn-jailbreak" and self._session_store and session:
                if getattr(rule, "_threshold", 0) > 0 and result.confidence >= rule.confidence_threshold:
                    self._session_store.reset(session.session_id)
            await self._apply_guard_result(result, ctx, "input")

        return ctx

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        """执行输出规则 + 行为规则（output 阶段）."""
        if not self._has_rules():
            return ctx

        session = self._get_session(ctx)
        output_text = ctx.response.content or ""
        if not output_text:
            return ctx

        model = ctx.response.model if hasattr(ctx.response, "model") else ""
        agent_id = getattr(ctx, "provider", "default")

        # 执行 output_rules
        for rule in self._output_rules:
            if not rule.is_enabled():
                continue
            if rule.scope and not ScopeMatcher.matches(rule.scope, model, agent_id):
                continue
            result = await rule.check_output(output_text, session=session)
            await self._apply_guard_result(result, ctx, "output")

        # 也执行 input_rules 的 check_output（如 content_safety 需要在两阶段检查）
        for rule in self._input_rules:
            if not rule.is_enabled():
                continue
            if rule.scope and not ScopeMatcher.matches(rule.scope, model, agent_id):
                continue
            result = await rule.check_output(output_text, session=session)
            await self._apply_guard_result(result, ctx, "output")

        # 执行 behavioral_rules (output 阶段)
        for rule in self._behavioral_rules:
            if not rule.is_enabled():
                continue
            result = await rule.check_output(output_text, session=session)
            await self._apply_guard_result(result, ctx, "output")

        return ctx

    async def on_stream_chunk(
        self, chunk: StreamChunk, ctx: StreamContext
    ) -> StreamChunk | None:
        """流式 chunk 检查 — 仅 input_rules."""
        if not self._input_rules:
            return chunk

        accumulated = ctx.accumulated_content
        if not accumulated:
            return chunk

        for rule in self._input_rules:
            if not rule.is_enabled():
                continue
            result = await rule.check_input(accumulated)
            await self._apply_guard_result(result, ctx, "stream")
        return chunk

    # --------------------------------------------------------------- action logic

    async def _apply_guard_result(
        self,
        result: GuardResult,
        ctx: RequestContext | ResponseContext | StreamContext,
        medium: str,
    ) -> None:
        """Apply a guard result: block, redact, or log."""
        if not result.matches:
            return

        ctx.guard_results.append(result)

        # 记录命中统计（按 action 类型分别计数）
        rule_stats = self._hit_stats.setdefault(result.rule_id, {"total": 0})
        rule_stats["total"] = rule_stats.get("total", 0) + 1
        action_key = result.action.value if hasattr(result.action, "value") else str(result.action)
        rule_stats[action_key] = rule_stats.get(action_key, 0) + 1

        # 记录内容安全分类统计
        if result.rule_id == "content-safety" and "category_counts" in result.metadata:
            cat_counts = result.metadata["category_counts"]
            if isinstance(cat_counts, dict):
                for cat, cnt in cat_counts.items():
                    if isinstance(cat, str) and isinstance(cnt, int):
                        self._category_stats[cat] = self._category_stats.get(cat, 0) + cnt

        # 审计记录
        if self._audit_logger:
            await self._audit_event(result, ctx, medium)

        if result.action == GuardAction.BLOCK:
            logger.warning(
                "guardrail_block",
                rule_id=result.rule_id,
                matches=result.matches[:5],
                confidence=result.confidence,
                medium=medium,
            )
            raise BlockException(
                rule_id=result.rule_id,
                reason=format_block_reason(
                    result.rule_id, result.matches, result.confidence
                ),
                status_code=403,
            )

        elif result.action == GuardAction.REDACT:
            if isinstance(ctx, (RequestContext, StreamContext)):
                apply_redact_to_messages(ctx.request.messages, result.matches)
            if isinstance(ctx, ResponseContext) and ctx.response.content:
                from .action import apply_redact
                new_content = apply_redact(ctx.response.content, result.matches)
                ctx.response.content = new_content

            logger.info(
                "guardrail_redact",
                rule_id=result.rule_id,
                match_count=len(result.matches),
            )

        elif result.action == GuardAction.LOG:
            logger.info(
                "guardrail_log",
                rule_id=result.rule_id,
                matches=result.matches[:5],
                confidence=result.confidence,
            )

    async def _audit_event(
        self,
        result: GuardResult,
        ctx: RequestContext | ResponseContext | StreamContext,
        medium: str,
    ) -> None:
        """记录审计事件."""
        import uuid
        if self._audit_logger is None:
            return
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            event_type=result.action.value if hasattr(result.action, "value") else str(result.action),
            rule_id=result.rule_id,
            trace_id=getattr(ctx, "trace_id", None),
            details=f"[{medium}] {result.details}",
        )
        await self._audit_logger.log_event(event)

    # ------------------------------------------------------------------- helpers

    def _has_rules(self) -> bool:
        return bool(self._input_rules or self._output_rules or self._behavioral_rules)

    def _get_session(
        self, ctx: RequestContext | ResponseContext | StreamContext
    ) -> SessionState | None:
        """从 context 获取或创建 session state."""
        if self._session_store is None:
            return None
        session_id = getattr(ctx, "trace_id", None) or getattr(ctx, "span_id", "default")
        return self._session_store.get_or_create(str(session_id))

    # ------------------------------------------------------------------- public

    @property
    def rules(self) -> list[BaseGuardRule]:
        return list(self._input_rules + self._output_rules + self._behavioral_rules)

    def get_stats(self) -> dict[str, dict[str, int]]:
        """Return per-rule hit counts broken down by action type.

        Returns:
            {rule_id: {"total": N, "block": N, "redact": N, "log": N}}
        """
        return {
            r.rule_id: self._hit_stats.get(r.rule_id, {"total": 0})
            for r in self.rules
        }

    def get_category_stats(self) -> dict[str, int]:
        """Return content safety category hit counts.

        Returns:
            {"violence": N, "self_harm": N, "illegal": N, "hate": N}
        """
        return dict(self._category_stats)
