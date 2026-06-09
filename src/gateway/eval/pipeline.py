"""EvalPipeline — runs heuristic evaluators on every response.

Implements Middleware interface for request/response hooks.
Heuristic evals run synchronously in on_response.
LLM-as-judge evals are queued asynchronously (non-blocking).
"""

from typing import Any, cast

from gateway.proxy.middleware import Middleware
from shared.models import (
    EvalResult,
    EvalMetrics,
    RequestContext,
    ResponseContext,
)
from shared.logging import get_logger
from .heuristic import (
    HeuristicEvaluator,
    ResponseLengthEval,
    RepetitionEval,
    LatencyEval,
    ToolCallEval,
)

logger = get_logger()


class EvalPipeline(Middleware):
    """Evaluates agent input/output quality across multiple dimensions.

    Heuristic evals (ResponseLength, Repetition, Latency, ToolCall) run
    synchronously in the response phase — fast, deterministic, zero-cost.

    LLM-as-Judge evals run asynchronously after the response is sent,
    so they never block the gateway.

    Priority: 90 — runs late, after guardrails and budget checks.
    """

    priority: int = 90
    # NOTE: declared as Any on the class (not Protocol) because basedpyright
    # strict mode rejects Protocol as an instance attribute. The actual
    # list[HeuristicEvaluator] type is preserved in __init__ assignment.
    _heuristic_evals: Any
    _llm_judge: object | None

    def __init__(
        self,
        max_response_length: int = 10_000,
        repetition_threshold: float = 0.3,
        latency_p99_threshold_ms: float = 5_000.0,
        llm_judge: object | None = None,
    ) -> None:
        self._heuristic_evals = cast(
            list[HeuristicEvaluator],
            [
                ResponseLengthEval(max_response_length=max_response_length),
                RepetitionEval(repetition_threshold=repetition_threshold),
                LatencyEval(p99_threshold_ms=latency_p99_threshold_ms),
                ToolCallEval(),
            ],
        )
        # LLM judge uses structural duck typing — store as object and use
        # cast at call sites to avoid leaking an optional Protocol type
        self._llm_judge = llm_judge

    # ---------------------------------------------------------------- Middleware

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        return ctx  # Nothing to evaluate on input (yet)

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        results: list[EvalResult] = []

        # Run heuristic evals
        for evaluator in self._heuristic_evals:
            try:
                if hasattr(evaluator, "name") and evaluator.name == "latency":  # type: ignore[union-attr]
                    latency_ms = getattr(ctx, "_latency_ms", 0.0)
                    result = evaluator.evaluate(ctx.response, latency_ms=latency_ms)  # type: ignore[union-attr]
                else:
                    result = evaluator.evaluate(ctx.response)  # type: ignore[union-attr]
                results.append(result)
            except Exception as e:
                logger.warning(
                    "eval_error",
                    evaluator=getattr(evaluator, "name", type(evaluator).__name__),
                    error=str(e),
                )

        ctx.eval_results = results

        # Log low-score evals
        low_scores = [r for r in results if r.score < 0.5]
        if low_scores:
            names = ", ".join(f"{r.name}={r.score:.2f}" for r in low_scores)
            logger.info("low_eval_scores", trace_id=ctx.trace_id, scores=names)

        # Queue async LLM judge if configured
        if self._llm_judge:
            try:
                import asyncio
                from typing import cast
                # Imported lazily here to avoid a hard dependency on
                # llm_judge module when it's not configured
                from gateway.eval.llm_judge import LLMJudgeEvaluator
                judge = cast(LLMJudgeEvaluator, self._llm_judge)
                asyncio.create_task(
                    judge.evaluate(ctx.request, ctx.response, ctx.trace_id)
                )
            except Exception as e:
                logger.debug("llm_judge_queue_error", error=str(e))

        return ctx
