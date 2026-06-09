"""GuardrailsEngine — main middleware that orchestrates all guardrail rules.

Implements the Middleware interface from proxy.middleware.
Processes requests before forwarding and responses after receiving.
"""

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
from .rules.pii import PIIDetectionRule
from .rules.injection import InjectionDetectionRule
from .rules.content import ContentSafetyRule
from .action import apply_redact_to_messages, format_block_reason

logger = get_logger()

# Rule-type → class mapping for factory creation
_RULE_FACTORY: dict[str, type[BaseGuardRule]] = {
    "pii": PIIDetectionRule,
    "injection": InjectionDetectionRule,
    "content": ContentSafetyRule,
}


class GuardrailsEngine(Middleware):
    """Guardrails middleware — high priority, runs early in the chain.

    Request phase  (priority=10): scans input messages before forwarding
    Response phase (priority=10): scans output content after receiving
    Stream phase:                   scans accumulated text chunk-by-chunk
    """

    priority: int = 10  # Run very early — guardrails before rate limiting, etc.

    def __init__(self, rule_configs: list[dict] | None = None) -> None:
        self._rules: list[BaseGuardRule] = []
        self._hit_stats: dict[str, int] = {}  # rule_id → hit count
        if rule_configs:
            self._load_rules(rule_configs)

    # -------------------------------------------------------------------- factory

    @classmethod
    def from_policy_store(cls, policy_store: object) -> "GuardrailsEngine":
        """Build a GuardrailsEngine from a PolicyStore."""
        config = policy_store.guardrails_config()  # type: ignore[union-attr]
        rule_dicts = [r.model_dump() for r in config.rules]
        return cls(rule_configs=rule_dicts)

    def _load_rules(self, rule_configs: list[dict]) -> None:
        """Instantiate rules from configuration dicts."""
        self._rules.clear()
        for cfg in rule_configs:
            if not cfg.get("enabled", True):
                continue

            rule_type = cfg.get("type", "")
            factory = _RULE_FACTORY.get(rule_type)
            if factory is None:
                logger.warning("unknown_rule_type", type=rule_type, id=cfg.get("id"))
                continue

            try:
                rule = factory(
                    confidence_threshold=cfg.get("confidence_threshold", 0.7),
                    enabled=cfg.get("enabled", True),
                    **{k: v for k, v in cfg.items()
                       if k not in ("id", "type", "action", "confidence_threshold", "enabled", "description")
                       and v is not None},
                )
                rule.rule_id = cfg.get("id", rule.rule_id)
                rule.action = GuardAction(cfg.get("action", rule.action.value))
                self._rules.append(rule)
                logger.debug("guard_rule_loaded", rule_id=rule.rule_id, type=rule_type)
            except Exception as e:
                logger.error("rule_init_error", rule_id=cfg.get("id"), error=str(e))

    # ---------------------------------------------------------------- Middleware

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        """Scan input messages for violations before forwarding."""
        if not self._rules:
            return ctx

        # Collect all message content as a single text for scanning
        texts = [m.content for m in ctx.request.messages if m.content]
        full_text = "\n".join(texts)

        for rule in self._rules:
            if not rule.is_enabled():
                continue
            result = await rule.check_input(full_text)
            await self._apply_guard_result(result, ctx, medium="input")
        return ctx

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        """Scan output content for violations after receiving."""
        if not self._rules:
            return ctx

        output_text = ctx.response.content or ""
        if not output_text:
            return ctx

        for rule in self._rules:
            if not rule.is_enabled():
                continue
            result = await rule.check_output(output_text)
            await self._apply_guard_result(result, ctx, medium="output")
        return ctx

    async def on_stream_chunk(self, chunk: StreamChunk, ctx: StreamContext) -> StreamChunk | None:
        """Check accumulated streaming content chunk-by-chunk.

        Only performs immediate checks (injection / content keywords).
        PII is deferred to final on_response in SSEInterceptor.finalize().
        """
        if not self._rules:
            return chunk

        accumulated = ctx.accumulated_content
        if not accumulated:
            return chunk

        for rule in self._rules:
            if not rule.is_enabled():
                continue
            # In stream mode, run input-phase checks on accumulated content
            # (output checks are deferred to stream close)
            result = await rule.check_input(accumulated)
            await self._apply_guard_result(result, ctx, medium="stream")
        return chunk

    # --------------------------------------------------------------- action logic

    async def _apply_guard_result(
        self,
        result: GuardResult,
        ctx: RequestContext | ResponseContext | StreamContext,
        *,
        medium: str,
    ) -> None:
        """Apply a guard result: block, redact, or log."""
        if not result.matches:
            return  # No matches — nothing to do

        # Record hit
        ctx.guard_results.append(result)
        self._hit_stats[result.rule_id] = self._hit_stats.get(result.rule_id, 0) + 1

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
                reason=format_block_reason(result.rule_id, result.matches, result.confidence),
                status_code=403,
            )

        elif result.action == GuardAction.REDACT:
            # Redact in-place
            if hasattr(ctx, "request") and hasattr(ctx.request, "messages"):
                apply_redact_to_messages(ctx.request.messages, result.matches)  # type: ignore[arg-type]
            if hasattr(ctx, "response") and ctx.response.content:  # type: ignore[union-attr]
                from .action import apply_redact
                new_content = apply_redact(ctx.response.content, result.matches)  # type: ignore[union-attr]
                ctx.response.content = new_content  # type: ignore[union-attr]

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

    # ------------------------------------------------------------------- public

    @property
    def rules(self) -> list[BaseGuardRule]:
        return list(self._rules)

    def get_stats(self) -> dict:
        """Return per-rule hit counts."""
        return {
            r.rule_id: self._hit_stats.get(r.rule_id, 0)
            for r in self._rules
        }
