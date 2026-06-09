"""Unit tests for trace store, engine, and span tree."""

import pytest
import uuid
from datetime import datetime, timezone

from gateway.trace.store import TraceStore
from gateway.trace.tree import SpanTree
from shared.models import TokenUsage


class TestTraceStore:
    """Tests for SQLite-backed TraceStore."""

    @pytest.fixture
    async def store(self, temp_db_path):
        s = TraceStore(db_path=temp_db_path)
        await s.initialize()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_create_and_get_trace(self, store):
        """Should create a trace and retrieve it."""
        trace_id = str(uuid.uuid4())
        await store.create_trace(trace_id, agent_id="test-agent")

        trace = await store.get_trace(trace_id)
        assert trace is not None
        assert trace["trace_id"] == trace_id
        assert trace["agent_id"] == "test-agent"
        assert trace["status"] == "ok"

    @pytest.mark.asyncio
    async def test_list_traces(self, store):
        """Should list traces in descending order."""
        tid1 = str(uuid.uuid4())
        tid2 = str(uuid.uuid4())
        await store.create_trace(tid1)
        await store.create_trace(tid2)

        traces = await store.list_traces(limit=10)
        assert len(traces) >= 2
        # Most recent first
        trace_ids = [t["trace_id"] for t in traces]
        assert tid2 in trace_ids

    @pytest.mark.asyncio
    async def test_create_and_get_span(self, store):
        """Should create a span and retrieve it."""
        from shared.models import TraceSpan

        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())

        await store.create_trace(trace_id)

        span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            provider="openai",
            model="gpt-4o",
            status="ok",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=123.4,
        )
        await store.create_span(span)

        retrieved = await store.get_span(span_id)
        assert retrieved is not None
        assert retrieved["span_id"] == span_id
        assert retrieved["provider"] == "openai"
        assert retrieved["model"] == "gpt-4o"
        assert retrieved["prompt_tokens"] == 10
        assert retrieved["completion_tokens"] == 5

    @pytest.mark.asyncio
    async def test_finish_span(self, store):
        """Should update span with final data."""
        from shared.models import TraceSpan

        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())

        await store.create_trace(trace_id)
        span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            provider="openai",
            model="gpt-4o",
        )
        await store.create_span(span)

        await store.finish_span(
            span_id=span_id,
            status="ok",
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            latency_ms=200.5,
            guard_hits=["injection-detection"],
            eval_scores={"relevance": 0.95},
        )

        retrieved = await store.get_span(span_id)
        assert retrieved["status"] == "ok"
        assert retrieved["prompt_tokens"] == 20
        assert retrieved["completion_tokens"] == 10
        assert retrieved["latency_ms"] == 200.5

    @pytest.mark.asyncio
    async def test_get_spans_for_trace(self, store):
        """Should retrieve all spans for a trace."""
        from shared.models import TraceSpan

        trace_id = str(uuid.uuid4())

        await store.create_trace(trace_id)

        for i in range(3):
            span = TraceSpan(
                trace_id=trace_id,
                span_id=str(uuid.uuid4()),
                provider="openai",
                model=f"gpt-4o-{i}",
            )
            await store.create_span(span)

        spans = await store.get_spans(trace_id)
        assert len(spans) == 3

    @pytest.mark.asyncio
    async def test_update_trace_stats(self, store):
        """Should update trace-level aggregated stats."""
        trace_id = str(uuid.uuid4())
        await store.create_trace(trace_id)

        await store.update_trace(
            trace_id=trace_id,
            total_tokens=100,
            total_latency_ms=500.0,
            status="ok",
        )

        trace = await store.get_trace(trace_id)
        assert trace["total_tokens"] == 100
        assert trace["total_latency_ms"] == 500.0


class TestSpanTree:
    """Tests for SpanTree builder."""

    def test_empty_spans(self):
        """Should return None for empty span list."""
        tree = SpanTree([])
        assert tree.build() is None

    def test_single_span(self):
        """Should build a single-node tree."""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "latency_ms": 100.0,
                "guard_hits": "[]",
                "eval_scores": "{}",
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = tree.build()
        assert root is not None
        assert root.span_id == "span1"
        assert root.total_tokens == 15
        assert root.subtree_tokens == 15

    def test_nested_spans(self):
        """Should build a tree with parent-child relationships."""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "latency_ms": 100.0,
                "guard_hits": "[]",
                "eval_scores": "{}",
                "created_at": "2024-01-01T00:00:00",
            },
            {
                "span_id": "span2",
                "trace_id": "trace1",
                "parent_span_id": "span1",
                "provider": "tool",
                "model": "calculator",
                "status": "ok",
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "latency_ms": 50.0,
                "guard_hits": "[]",
                "eval_scores": "{}",
                "created_at": "2024-01-01T00:00:01",
            },
        ]
        tree = SpanTree(spans)
        root = tree.build()
        assert root is not None
        assert len(root.children) == 1
        assert root.children[0].span_id == "span2"
        assert root.children[0].depth == 1
        assert root.subtree_tokens == 15 + 8  # parent + child
        assert root.subtree_latency_ms == 150.0

    def test_to_dict(self):
        """Should serialize tree to nested dict."""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "latency_ms": 100.0,
                "guard_hits": '["pii-detection"]',
                "eval_scores": '{"relevance": 0.9}',
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = tree.build()
        result = SpanTree.to_dict(root)
        assert result is not None
        assert result["span_id"] == "span1"
        assert result["total_tokens"] == 15
        assert result["guard_hits"] == ["pii-detection"]
        assert result["eval_scores"] == {"relevance": 0.9}
        assert result["children"] == []
