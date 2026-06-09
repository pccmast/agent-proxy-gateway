"""TraceEngine — generates trace/span IDs, manages span lifecycle, and stores trace data."""

from __future__ import annotations

import time
import uuid
from contextvars import ContextVar
from typing import Any

from fastapi import Request

from shared.models import TokenUsage, TraceSpan
from shared.logging import get_logger
from .store import TraceStore
from .tree import SpanTree

logger = get_logger()

# Context variables for async-safe trace/span context propagation
_current_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_current_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)
_current_agent_id: ContextVar[str | None] = ContextVar("agent_id", default=None)


class TraceEngine:
    """Generates and manages traces and spans for the gateway.

    Each incoming request gets a unique trace_id and span_id.
    Nested calls (e.g., Agent → LLM → Tool → LLM) form a span tree
    connected via parent_span_id.

    Uses contextvars for async-safe context propagation.
    """

    def __init__(self, store: TraceStore) -> None:
        self._store = store
        self._span_start_times: dict[str, float] = {}

    @property
    def store(self) -> TraceStore:
        return self._store

    # --- Context management ---

    @staticmethod
    def get_current_trace_id() -> str | None:
        return _current_trace_id.get()

    @staticmethod
    def get_current_span_id() -> str | None:
        return _current_span_id.get()

    @staticmethod
    def set_context(trace_id: str, span_id: str, agent_id: str | None = None) -> None:
        _current_trace_id.set(trace_id)
        _current_span_id.set(span_id)
        if agent_id is not None:
            _current_agent_id.set(agent_id)

    @staticmethod
    def clear_context() -> None:
        _current_trace_id.set(None)
        _current_span_id.set(None)
        _current_agent_id.set(None)

    # --- Trace lifecycle ---

    async def start_trace(self, request: Request) -> tuple[str, str]:
        """Start a new trace for an incoming request.

        Returns:
            (trace_id, span_id) tuple.
        """
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        agent_id = request.headers.get("X-Agent-ID")
        _current_agent_id.set(agent_id)

        self.set_context(trace_id, span_id, agent_id)
        self._span_start_times[span_id] = time.monotonic()

        await self._store.create_trace(trace_id, agent_id=agent_id)

        logger.debug(
            "trace_started",
            trace_id=trace_id,
            span_id=span_id,
            agent_id=agent_id,
            path=request.url.path,
        )
        return trace_id, span_id

    async def start_span(
        self,
        provider: str,
        model: str,
        parent_span_id: str | None = None,
        request_hash: str = "",
    ) -> str:
        """Start a child span (e.g., for nested tool calls).

        Returns:
            new span_id.
        """
        trace_id = self.get_current_trace_id()
        if trace_id is None:
            raise RuntimeError("No active trace — call start_trace() first")

        span_id = str(uuid.uuid4())
        parent_id = parent_span_id or self.get_current_span_id()

        self.set_context(trace_id, span_id)
        self._span_start_times[span_id] = time.monotonic()

        # Create initial span record
        span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_id,
            provider=provider,
            model=model,
            request_hash=request_hash,
        )
        await self._store.create_span(span)

        return span_id

    async def finish_span(
        self,
        trace_id: str,
        span_id: str,
        status: str = "ok",
        token_usage: TokenUsage | None = None,
        guard_hits: list[str] | None = None,
        eval_scores: dict[str, float] | None = None,
    ) -> None:
        """Finish a span with final data."""
        latency_ms = self._compute_latency(span_id)
        await self._store.finish_span(
            span_id=span_id,
            status=status,
            token_usage=token_usage,
            latency_ms=latency_ms,
            guard_hits=guard_hits,
            eval_scores=eval_scores,
        )

        # Update trace-level aggregates
        await self._update_trace_stats(trace_id)

        logger.debug(
            "span_finished",
            trace_id=trace_id,
            span_id=span_id,
            status=status,
            latency_ms=round(latency_ms, 1),
        )

    # --- Query methods ---

    async def get_trace(self, trace_id: str) -> dict | None:
        """Get trace metadata."""
        return await self._store.get_trace(trace_id)

    async def list_traces(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List recent traces."""
        return await self._store.list_traces(limit=limit, offset=offset)

    async def get_span_tree(self, trace_id: str) -> dict | None:
        """Get the full span tree for a trace."""
        spans = await self._store.get_spans(trace_id)
        if not spans:
            return None
        tree = SpanTree(spans)
        root = tree.build()
        return SpanTree.to_dict(root)

    async def get_stats(self, hours: int = 24) -> dict:
        """Get aggregate statistics."""
        return await self._store.get_stats(hours=hours)

    # --- Internal helpers ---

    def _compute_latency(self, span_id: str) -> float:
        """Compute elapsed time in milliseconds for a span."""
        start = self._span_start_times.pop(span_id, None)
        if start is None:
            return 0.0
        return (time.monotonic() - start) * 1000.0

    async def _update_trace_stats(self, trace_id: str) -> None:
        """Aggregate all span stats into the trace record."""
        spans = await self._store.get_spans(trace_id)
        if not spans:
            return

        total_tokens = 0
        total_latency = 0.0
        final_status = "ok"
        status_priority = {"error": 0, "timeout": 1, "blocked": 2, "ok": 3}

        for s in spans:
            total_tokens += int(s.get("prompt_tokens", 0)) + int(s.get("completion_tokens", 0))
            total_latency += float(s.get("latency_ms", 0))
            s_status = s.get("status", "ok")
            if status_priority.get(s_status, 3) < status_priority.get(final_status, 3):
                final_status = s_status

        await self._store.update_trace(
            trace_id=trace_id,
            total_tokens=total_tokens,
            total_latency_ms=total_latency,
            status=final_status,
        )
