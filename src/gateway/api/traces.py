"""Trace API — endpoints for viewing traces and span trees."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query


def create_trace_router(trace_engine: Any) -> APIRouter:
    """Create a FastAPI router for trace management endpoints.

    Args:
        trace_engine: TraceEngine instance.

    Returns:
        APIRouter configured with trace endpoints.
    """
    router = APIRouter(prefix="/api/traces", tags=["traces"])

    @router.get("")
    async def list_traces(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        """List recent traces."""
        traces = await trace_engine.list_traces(limit=limit, offset=offset)
        return {"traces": traces, "count": len(traces)}

    @router.get("/{trace_id}")
    async def get_trace(trace_id: str):
        """Get trace metadata and full span tree."""
        trace = await trace_engine.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")

        span_tree = await trace_engine.get_span_tree(trace_id, load_content=True)
        return {"trace": trace, "span_tree": span_tree}

    @router.get("/{trace_id}/spans")
    async def get_spans(trace_id: str):
        """Get flat span list for a trace (alternative to tree view)."""
        trace = await trace_engine.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")

        span_tree = await trace_engine.get_span_tree(trace_id)
        return {"spans": span_tree}

    @router.get("/stats")
    async def get_stats(hours: int = Query(default=24, ge=1, le=720)):
        """Get aggregate trace statistics for the last N hours."""
        stats = await trace_engine.get_stats(hours=hours)
        return stats

    return router
