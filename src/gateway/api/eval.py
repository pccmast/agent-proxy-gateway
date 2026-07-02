"""Eval API — endpoints for evaluation results and metrics."""

from typing import Any

from fastapi import APIRouter, Query


def create_eval_router(eval_pipeline: Any = None) -> APIRouter:
    """Create a FastAPI router for eval results."""
    router = APIRouter(prefix="/api/eval", tags=["eval"])

    @router.get("/metrics")
    async def get_metrics():
        """Get eval metrics definition."""
        return {
            "metrics": [
                {"name": "response_length", "type": "heuristic", "description": "Response length quality (0-1)"},
                {
                    "name": "repetition",
                    "type": "heuristic",
                    "description": "Content repetition score (0-1, 1=no repetition)",
                },
                {"name": "latency", "type": "heuristic", "description": "Response latency score (0-1)"},
                {"name": "tool_call", "type": "heuristic", "description": "Tool call quality score (0-1)"},
                {"name": "relevance", "type": "llm_judge", "description": "LLM-judged relevance (0-1)"},
                {"name": "safety", "type": "llm_judge", "description": "LLM-judged safety (0-1)"},
                {"name": "coherence", "type": "llm_judge", "description": "LLM-judged coherence (0-1)"},
            ]
        }

    @router.get("/results")
    async def get_results(
        agent_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        """Get recent eval results. (Requires trace store integration for full data.)"""
        return {
            "results": [],
            "note": "Eval results are stored per-span. Use GET /api/traces/{id} to see eval_scores in span tree.",
        }

    return router
