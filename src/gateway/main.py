"""FastAPI application entry point — wires all gateway components together."""

# pyright: reportAny=false, reportUnusedFunction=false, reportUnusedCallResult=false
# pyright: reportUnusedParameter=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# Rationale: FastAPI app.state is inherently untyped (dict[str, Any]). Route handlers
# registered via decorators are "unused" from the linter's perspective but are accessed
# by FastAPI at runtime. Adding Protocol/TypeVar chains adds complexity without benefit.

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared.config import load_config
from shared.logging import setup_logging, get_logger

from gateway.adapter.normalizer import create_registry
from gateway.proxy.core import ProxyEngine
from gateway.proxy.middleware import MiddlewareChain
from gateway.trace.store import TraceStore
from gateway.trace.engine import TraceEngine
from gateway.policy.store import PolicyStore
from gateway.guardrails.engine import GuardrailsEngine
from gateway.guardrails.sqlite_session import SQLiteSessionStore
from gateway.budget.rate_limiter import SlidingWindowRateLimiter, RateLimitConfig
from gateway.budget.token_counter import TokenCounter
from gateway.budget.circuit_breaker import CircuitBreaker
from gateway.budget.request_timeout import RequestTimeoutGuard
from gateway.eval.pipeline import EvalPipeline
from gateway.eval.llm_judge import LLMJudgeEvaluator

setup_logging()
logger = get_logger()

# Global state
_proxy_engine: ProxyEngine | None = None
_trace_engine: TraceEngine | None = None
_trace_store: TraceStore | None = None
_policy_store: PolicyStore | None = None
_guardrails_engine: GuardrailsEngine | None = None
_rate_limiter: SlidingWindowRateLimiter | None = None
_token_counter: TokenCounter | None = None
_circuit_breaker: CircuitBreaker | None = None
_eval_pipeline: EvalPipeline | None = None
_cleanup_task: asyncio.Task[object] | None = None


async def _cleanup_abandoned_spans_loop(
    trace_engine: TraceEngine, interval_seconds: float = 300.0
) -> None:
    """Background task: periodically mark orphan spans as abandoned."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            marked = await trace_engine.cleanup_abandoned_spans(
                abandoned_minutes=5,
            )
            if marked > 0:
                logger.info("abandoned_spans_cleanup_completed", marked=marked)
        except asyncio.CancelledError:
            logger.info("cleanup_task_cancelled")
            return
        except Exception:
            logger.warning("cleanup_task_error", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _proxy_engine, _trace_engine, _trace_store, _policy_store
    global _guardrails_engine, _rate_limiter, _token_counter, _circuit_breaker, _eval_pipeline
    global _cleanup_task

    os.makedirs("data", exist_ok=True)

    # Policy
    _policy_store = PolicyStore(config_dir=app.state.settings.config_dir)
    _policy_store.reload()

    # Trace
    _trace_store = TraceStore(db_path=app.state.settings.db_path)
    await _trace_store.initialize()
    _trace_engine = TraceEngine(store=_trace_store)

    # Adapters
    adapter_registry = create_registry()

    # Guardrails
    if _policy_store.guardrails_config().enabled:
        _guardrails_engine = GuardrailsEngine.from_policy_store(_policy_store)
    else:
        _guardrails_engine = GuardrailsEngine()

    # Session store — persisted in SQLite so behavioural state
    # (jailbreak scores, tool-call history) survives restarts.
    # Uses a separate sync sqlite3 connection to avoid asyncio bridge issues.
    session_store = SQLiteSessionStore(db_path=app.state.settings.db_path)
    session_store.initialize()
    _guardrails_engine._session_store = session_store

    # Budget
    budget_cfg = _policy_store.budget_config()
    _token_counter = TokenCounter(
        max_tokens_per_hour=budget_cfg.defaults.max_tokens_per_hour,
        max_tokens_per_day=budget_cfg.defaults.max_tokens_per_day,
        warning_threshold=budget_cfg.defaults.warning_threshold,
    )

    rate_cfg = _policy_store.rate_limit_config()
    _rate_limiter = SlidingWindowRateLimiter(
        default_rpm=rate_cfg.defaults.rpm,
        default_tpm=rate_cfg.defaults.tpm,
    )
    for model_key, cfg in (rate_cfg.per_model or {}).items():
        _rate_limiter._per_model[model_key] = RateLimitConfig(rpm=cfg.rpm, tpm=cfg.tpm)

    cb_cfg = _policy_store.policy.circuit_breaker
    _circuit_breaker = CircuitBreaker(
        failure_threshold=cb_cfg.failure_threshold,
        recovery_timeout=float(cb_cfg.recovery_timeout),
        half_open_max_calls=cb_cfg.half_open_max_calls,
    )

    # Eval — heuristic checks only by default.
    # LLM-as-Judge is NOT recommended for production gateway deployments:
    # it consumes real token costs and belongs in a separate offline
    # evaluation system.  See eval/llm_judge.py for details.
    eval_cfg = _policy_store.eval_config()
    llm_judge = None
    if eval_cfg.llm_judge.enabled:
        api_key = app.state.settings.eval_llm_api_key
        if not api_key:
            # Fallback: read from env var specified in config
            api_key = os.environ.get(eval_cfg.llm_judge.api_key_env, "")
        if api_key:
            llm_judge = LLMJudgeEvaluator(model=eval_cfg.llm_judge.model, api_key=api_key, sample_rate=eval_cfg.llm_judge.sample_rate)
    _eval_pipeline = EvalPipeline(
        max_response_length=eval_cfg.heuristic.max_response_length,
        repetition_threshold=eval_cfg.heuristic.repetition_threshold,
        latency_p99_threshold_ms=float(eval_cfg.heuristic.latency_p99_threshold_ms),
        llm_judge=llm_judge,
    )

    # Middleware chain
    chain = MiddlewareChain()
    chain.add(RequestTimeoutGuard(total_timeout_seconds=60.0))
    if _guardrails_engine.rules:
        chain.add(_guardrails_engine)
    chain.add(_rate_limiter)
    chain.add(_eval_pipeline)

    # Proxy
    _proxy_engine = ProxyEngine(
        settings=app.state.settings,
        adapter_registry=adapter_registry,
        middleware_chain=chain,
        trace_engine=_trace_engine,
        circuit_breaker=_circuit_breaker,
    )

    # State
    app.state.proxy_engine = _proxy_engine
    app.state.trace_engine = _trace_engine
    app.state.guardrails_engine = _guardrails_engine
    app.state.token_counter = _token_counter
    app.state.rate_limiter = _rate_limiter
    app.state.circuit_breaker = _circuit_breaker
    app.state.eval_pipeline = _eval_pipeline

    logger.info("gateway_initialized", providers=adapter_registry.list_providers())

    # Start background abandoned-span cleanup task
    _cleanup_task = asyncio.create_task(
        _cleanup_abandoned_spans_loop(_trace_engine, interval_seconds=300.0)
    )

    yield

    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        _cleanup_task = None
    if _proxy_engine:
        await _proxy_engine.close()
    if _trace_store:
        await _trace_store.close()


def create_app() -> FastAPI:
    config = load_config()
    app = FastAPI(title="Agent Proxy Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = config

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0", "host": config.host, "port": config.port}

    @app.get("/metrics")
    async def metrics():
        from gateway.metrics import metrics_response
        from fastapi.responses import Response
        return Response(content=metrics_response(), media_type="text/plain")

    # Traces
    @app.api_route("/api/traces/stats", methods=["GET"], tags=["traces"])
    async def get_stats(hours: int = 24):
        e = getattr(app.state, "trace_engine", None)
        if not e: return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        return await e.get_stats(hours=hours)

    @app.api_route("/api/traces", methods=["GET"], tags=["traces"])
    async def list_traces(limit: int = 50, offset: int = 0):
        e = getattr(app.state, "trace_engine", None)
        if not e: return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        traces = await e.list_traces(limit=limit, offset=offset)
        return {"traces": traces, "count": len(traces)}

    @app.api_route("/api/traces/{trace_id}", methods=["GET"], tags=["traces"])
    async def get_trace(trace_id: str):
        e = getattr(app.state, "trace_engine", None)
        if not e: return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        trace = await e.get_trace(trace_id)
        if not trace: return JSONResponse(status_code=404, content={"error": "Trace not found"})
        return {"trace": trace, "span_tree": await e.get_span_tree(trace_id)}

    # Guardrails
    @app.api_route("/api/guardrails/stats", methods=["GET"], tags=["guardrails"])
    async def guardrails_stats():
        ge = getattr(app.state, "guardrails_engine", None)
        if not ge: return JSONResponse(status_code=503, content={"error": "Guardrails not enabled"})
        s = ge.get_stats()
        # v2: stats 结构为 {rule_id: {"total": N, "block": N, ...}}
        total_hits = sum(v.get("total", 0) for v in s.values())
        return {"stats": s, "total_hits": total_hits}

    @app.api_route("/api/guardrails/rules", methods=["GET"], tags=["guardrails"])
    async def guardrails_rules():
        ge = getattr(app.state, "guardrails_engine", None)
        if not ge: return JSONResponse(status_code=503, content={"error": "Guardrails not enabled"})
        return {"rules": [{"id": r.rule_id, "action": r.action.value, "enabled": r.enabled} for r in ge.rules]}

    # Budget
    @app.api_route("/api/budget/status", methods=["GET"], tags=["budget"])
    async def budget_status(agent_id: str = "default"):
        tc = getattr(app.state, "token_counter", None)
        if not tc: return JSONResponse(status_code=503, content={"error": "Budget not configured"})
        return tc.check_budget(agent_id) if agent_id else {"agents": tc.get_status()}

    # Eval
    @app.api_route("/api/eval/metrics", methods=["GET"], tags=["eval"])
    async def eval_metrics():
        return {"metrics": ["response_length", "repetition", "latency", "tool_call", "relevance", "safety", "coherence"]}

    # Catch-all
    @app.api_route("/{path:path}", methods=["POST", "GET", "PUT", "DELETE", "PATCH"])
    async def proxy_catchall(request: Request, path: str):
        engine = getattr(app.state, "proxy_engine", None)
        if not engine: return JSONResponse(status_code=503, content={"error": "Proxy engine not ready"})
        return await engine.handle_request(request)

    return app


def run_server() -> None:
    import uvicorn
    config = load_config()
    # reload=True is dev-only and breaks in containers (watcher exits code 0)
    uvicorn.run("gateway.main:create_app", factory=True, host=config.host, port=config.port)


def validate_config() -> None:
    """CLI entry point: validate config/*.yaml files without starting the server.

    Usage: uv run validate-config
    """
    import sys
    from gateway.policy.store import PolicyStore

    config_dir = os.environ.get("GATEWAY_CONFIG_DIR", "./config")
    try:
        store = PolicyStore(config_dir=config_dir)
        store.reload()
        policy = store.policy
        print(f"Config OK — {len(policy.guardrails.rules)} guardrail rules, "
              f"{len(policy.proxy.providers)} provider(s)")
        sys.exit(0)
    except Exception as e:
        print(f"Config ERROR: {e}", file=sys.stderr)
        sys.exit(1)


app = create_app()

if __name__ == "__main__":
    run_server()
