"""Trace module — distributed tracing for request lifecycle observability."""

from .engine import TraceEngine
from .store import TraceStore
from .tree import SpanTree, SpanNode
from shared.models import (
    SpanStartParams,
    SpanFinishParams,
    GuardHitRecord,
    EvalScoreRecord,
    SpanContent,
)

__all__ = [
    "TraceEngine",
    "TraceStore",
    "SpanTree",
    "SpanNode",
    "SpanStartParams",
    "SpanFinishParams",
    "GuardHitRecord",
    "EvalScoreRecord",
    "SpanContent",
]
