"""Budget API — endpoints for budget status and rate limit information."""

from typing import Any

from fastapi import APIRouter, Query


def create_budget_router(
    token_counter: Any,
    rate_limiter: Any = None,
    circuit_breaker: Any = None,
) -> APIRouter:
    """Create a FastAPI router for budget management endpoints."""
    router = APIRouter(prefix="/api/budget", tags=["budget"])

    @router.get("/status")
    async def budget_status(agent_id: str | None = None):
        """Get budget usage for a specific agent or all agents."""
        if agent_id:
            status = token_counter.check_budget(agent_id)
            return status
        return {"agents": token_counter.get_status()}

    @router.get("/rate-limits")
    async def rate_limits():
        """Get current rate limit status."""
        if rate_limiter is None:
            return {"status": "Rate limiter not configured"}
        return rate_limiter.get_status()

    @router.get("/circuit-breaker")
    async def circuit_breaker_status():
        """Get circuit breaker state."""
        if circuit_breaker is None:
            return {"status": "Circuit breaker not configured"}
        return circuit_breaker.get_status()

    @router.post("/circuit-breaker/reset")
    async def reset_circuit_breaker():
        """Force reset circuit breaker to CLOSED state."""
        if circuit_breaker is None:
            return {"status": "Circuit breaker not configured"}
        circuit_breaker.reset()
        return {"status": "reset", "state": circuit_breaker.state.value}

    return router
