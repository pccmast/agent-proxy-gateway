"""FastAPI application entry point."""

from fastapi import FastAPI
from shared.config import load_config
from shared.logging import setup_logging, get_logger

setup_logging()
logger = get_logger()


def create_app() -> FastAPI:
    """Create and configure the gateway FastAPI application."""
    config = load_config()

    app = FastAPI(
        title="Agent Proxy Gateway",
        description="Transparent proxy gateway for AI Agent observability and control",
        version="0.1.0",
    )

    # Health check
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    # TODO: Register proxy routes (catch-all path)
    # TODO: Register management API routes
    # TODO: Initialize middleware chain
    # TODO: Initialize adapter registry
    # TODO: Initialize trace engine
    # TODO: Initialize guardrails engine

    logger.info("gateway_started", host=config.host, port=config.port)
    return app


def run_server() -> None:
    """Run the gateway server with uvicorn."""
    import uvicorn
    from shared.config import load_config

    config = load_config()
    uvicorn.run(
        "gateway.main:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        reload=True,
    )


app = create_app()