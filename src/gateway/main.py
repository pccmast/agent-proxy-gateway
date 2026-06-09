"""FastAPI application entry point — wires all gateway components together."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared.config import load_config, GatewaySettings
from shared.logging import setup_logging, get_logger

from gateway.adapter.normalizer import create_registry
from gateway.proxy.core import ProxyEngine
from gateway.proxy.middleware import MiddlewareChain
from gateway.trace.store import TraceStore
from gateway.trace.engine import TraceEngine
from gateway.api.traces import create_trace_router

import os

setup_logging()
logger = get_logger()

# Global state for lifespan management
_proxy_engine: ProxyEngine | None = None
_trace_engine: TraceEngine | None = None
_trace_store: TraceStore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — initialize and cleanup resources."""
    global _proxy_engine, _trace_engine, _trace_store

    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)

    # Initialize trace store
    logger.info("initializing_trace_store")
    _trace_store = TraceStore(db_path=app.state.settings.db_path)
    await _trace_store.initialize()

    # Initialize trace engine
    _trace_engine = TraceEngine(store=_trace_store)

    # Initialize adapter registry
    adapter_registry = create_registry()

    # Initialize middleware chain (empty for now, guardrails/budget/eval added in later sprints)
    middleware_chain = MiddlewareChain()

    # Initialize proxy engine
    _proxy_engine = ProxyEngine(
        settings=app.state.settings,
        adapter_registry=adapter_registry,
        middleware_chain=middleware_chain,
        trace_engine=_trace_engine,
    )

    app.state.proxy_engine = _proxy_engine
    app.state.trace_engine = _trace_engine

    logger.info(
        "gateway_initialized",
        host=app.state.settings.host,
        port=app.state.settings.port,
        providers=adapter_registry.list_providers(),
    )

    yield

    # Cleanup
    logger.info("shutting_down")
    if _proxy_engine:
        await _proxy_engine.close()
    if _trace_store:
        await _trace_store.close()


def create_app() -> FastAPI:
    """Create and configure the gateway FastAPI application."""
    config = load_config()

    app = FastAPI(
        title="Agent Proxy Gateway",
        description="Transparent proxy gateway for AI Agent observability and control",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.settings = config

    # --- Health check ---
    @app.get("/health")
    async def health():
        proxies = app.state.settings.openai_api_key != ""
        return {
            "status": "ok",
            "version": "0.1.0",
            "proxies_configured": proxies,
        }

    # --- Management API routes ---
    @app.get("/api/ping")
    async def ping():
        trace_engine: TraceEngine | None = getattr(app.state, "trace_engine", None)
        store_ok = False
        if trace_engine:
            try:
                await trace_engine.list_traces(limit=1)
                store_ok = True
            except Exception:
                pass
        return {"ping": "pong", "db": "ok" if store_ok else "unavailable"}

    # Register trace API router
    # Note: trace_engine is available as app.state.trace_engine after startup
    # We create the router lazily via a wrapper

    async def _get_trace_engine():
        return getattr(app.state, "trace_engine", None)

    @app.api_route("/api/traces", methods=["GET"], tags=["traces"])
    async def list_traces(limit: int = 50, offset: int = 0):
        engine = await _get_trace_engine()
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        traces = await engine.list_traces(limit=limit, offset=offset)
        return {"traces": traces, "count": len(traces)}

    @app.api_route("/api/traces/{trace_id}", methods=["GET"], tags=["traces"])
    async def get_trace(trace_id: str):
        engine = await _get_trace_engine()
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        trace = await engine.get_trace(trace_id)
        if trace is None:
            return JSONResponse(status_code=404, content={"error": "Trace not found"})
        span_tree = await engine.get_span_tree(trace_id)
        return {"trace": trace, "span_tree": span_tree}

    @app.api_route("/api/traces/stats", methods=["GET"], tags=["traces"])
    async def get_stats(hours: int = 24):
        engine = await _get_trace_engine()
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "Trace engine not ready"})
        stats = await engine.get_stats(hours=hours)
        return stats

    # --- Catch-all proxy route ---
    @app.api_route("/{path:path}", methods=["POST", "GET", "PUT", "DELETE", "PATCH"])
    async def proxy_catchall(request: Request, path: str):
        """Catch-all route that forwards all traffic through the proxy engine."""
        engine: ProxyEngine | None = getattr(app.state, "proxy_engine", None)
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "Proxy engine not ready"})
        return await engine.handle_request(request)

    logger.info("app_created")
    return app


def run_server() -> None:
    """Run the gateway server with uvicorn."""
    import uvicorn

    config = load_config()
    uvicorn.run(
        "gateway.main:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=True,
    )


app = create_app()
