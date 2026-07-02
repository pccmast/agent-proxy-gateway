"""Guardrails API — endpoints for rule listing and hit statistics."""

from typing import Any

from fastapi import APIRouter, HTTPException


def create_guardrails_router(
    guardrails_engine: Any,
    policy_store: Any = None,
) -> APIRouter:
    """Create a FastAPI router for guardrails management endpoints.

    Args:
        guardrails_engine: GuardrailsEngine instance.
        policy_store: Optional PolicyStore for reading raw rule configs.

    Returns:
        APIRouter configured with guardrails endpoints.
    """
    router = APIRouter(prefix="/api/guardrails", tags=["guardrails"])

    @router.get("/stats")
    async def get_stats():
        """Get per-rule hit statistics."""
        stats = guardrails_engine.get_stats()
        # v2: stats 结构为 {rule_id: {"total": N, "block": N, ...}}
        total_hits = sum(v.get("total", 0) if isinstance(v, dict) else v for v in stats.values())
        return {"stats": stats, "total_hits": total_hits}

    @router.get("/rules")
    async def list_rules():
        """List all active guardrail rules with their current state."""
        rules = []
        for rule in guardrails_engine.rules:
            rules.append(
                {
                    "id": rule.rule_id,
                    "action": rule.action.value if hasattr(rule.action, "value") else str(rule.action),
                    "confidence_threshold": rule.confidence_threshold,
                    "enabled": rule.enabled,
                }
            )
        return {"rules": rules, "count": len(rules)}

    @router.post("/rules/{rule_id}/toggle")
    async def toggle_rule(rule_id: str):
        """Enable or disable a specific rule."""
        for rule in guardrails_engine.rules:
            if rule.rule_id == rule_id:
                rule.enabled = not rule.enabled
                return {
                    "rule_id": rule_id,
                    "enabled": rule.enabled,
                }
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    return router
