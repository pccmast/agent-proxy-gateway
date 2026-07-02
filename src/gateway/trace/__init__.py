"""Trace module — distributed tracing for request lifecycle observability."""

from shared.models import (
    EvalScoreRecord,
    GuardHitRecord,
    SpanContent,
    SpanFinishParams,
    SpanStartParams,
)

from .engine import TraceEngine
from .store import TraceStore
from .tree import SpanNode, SpanTree

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
