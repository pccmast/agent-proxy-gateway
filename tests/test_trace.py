"""Unit tests for trace store, engine, and span tree — v2 schema."""

import json
import uuid

import pytest

from gateway.trace.store import TraceStore
from gateway.trace.tree import SpanTree
from shared.models import (
    EvalScoreRecord,
    GuardHitRecord,
    SpanFinishParams,
    SpanStartParams,
    TokenUsage,
    TraceSpan,
)

# ==========================================================================
# TestTraceStore — 存储层测试
# ==========================================================================


class TestTraceStore:
    """Tests for SQLite-backed TraceStore (v2 schema)."""

    @pytest.fixture
    async def store(self, temp_db_path):
        s = TraceStore(db_path=temp_db_path)
        await s.initialize()
        yield s
        await s.close()

    # ── 原有测试 (保持兼容) ──

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
        trace_ids = [t["trace_id"] for t in traces]
        assert tid2 in trace_ids

    @pytest.mark.asyncio
    async def test_create_and_get_span(self, store):
        """Should create a span and retrieve it."""
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
            guard_hits_json=json.dumps(
                [
                    {
                        "rule_id": "injection-detection",
                        "action": "block",
                        "matches": [],
                        "confidence": 0.95,
                        "details": "",
                    }
                ],
                ensure_ascii=False,
            ),
            eval_scores_json=json.dumps(
                [{"name": "relevance", "score": 0.95, "details": ""}],
                ensure_ascii=False,
            ),
        )

        retrieved = await store.get_span(span_id)
        assert retrieved["status"] == "ok"
        assert retrieved["prompt_tokens"] == 20
        assert retrieved["completion_tokens"] == 10
        assert retrieved["latency_ms"] == 200.5

    @pytest.mark.asyncio
    async def test_get_spans_for_trace(self, store):
        """Should retrieve all spans for a trace."""
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
            estimated_cost_usd=0.05,
            status="ok",
        )

        trace = await store.get_trace(trace_id)
        assert trace["total_tokens"] == 100
        assert trace["total_latency_ms"] == 500.0
        assert trace["estimated_cost_usd"] == 0.05

    # ── 新增: Schema 迁移 ──

    @pytest.mark.asyncio
    async def test_migration_idempotent(self, temp_db_path):
        """多次 initialize 不报错，不重复创建列"""
        s = TraceStore(db_path=temp_db_path)
        try:
            await s.initialize()
            await s.initialize()
            await s.initialize()
            # No error = pass
        finally:
            await s.close()

    @pytest.mark.asyncio
    async def test_new_schema_columns_exist(self, store):
        """P0-P3 新增列全部存在"""
        async with store.db.execute("PRAGMA table_info(traces)") as cursor:
            trace_cols = {row[1] async for row in cursor}
        async with store.db.execute("PRAGMA table_info(spans)") as cursor:
            span_cols = {row[1] async for row in cursor}

        # traces 新增列
        assert "session_id" in trace_cols
        assert "client_ip" in trace_cols
        assert "user_agent" in trace_cols
        assert "estimated_cost_usd" in trace_cols

        # spans P0 列
        assert "finish_reason" in span_cols
        assert "error_message" in span_cols
        assert "temperature" in span_cols
        assert "max_tokens" in span_cols
        assert "tool_calls_json" in span_cols
        assert "content_id" in span_cols
        # spans P1 列
        assert "ttft_ms" in span_cols
        assert "is_stream" in span_cols

    # ── 新增: P0 内容存储 ──

    @pytest.mark.asyncio
    async def test_finish_span_with_error_message(self, store):
        """error_message 正确写入和读取"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.finish_span(span_id=span_id, status="error", error_message="Connection refused")
        span = await store.get_span(span_id)
        assert span["error_message"] == "Connection refused"

    @pytest.mark.asyncio
    async def test_finish_span_with_finish_reason(self, store):
        """finish_reason 正确写入"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.finish_span(span_id=span_id, finish_reason="stop")
        span = await store.get_span(span_id)
        assert span["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_finish_span_with_request_params(self, store):
        """temperature / max_tokens 正确写入"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.finish_span(span_id=span_id, temperature=0.7, max_tokens=256)
        span = await store.get_span(span_id)
        assert span["temperature"] == 0.7
        assert span["max_tokens"] == 256

    @pytest.mark.asyncio
    async def test_span_content_insert_and_get(self, store):
        """span_contents 正确写入和读取"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        content_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.insert_span_content(content_id, span_id, "req_body", "resp_body")
        content = await store.get_span_content(content_id)
        assert content is not None
        assert content["request_body"] == "req_body"
        assert content["response_body"] == "resp_body"

    # ── 新增: P1 成本和性能 ──

    @pytest.mark.asyncio
    async def test_ttft_ms_handling(self, store):
        """ttft_ms 正确写入和读取"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.finish_span(span_id=span_id, ttft_ms=150.0)
        span = await store.get_span(span_id)
        assert span["ttft_ms"] == 150.0

    @pytest.mark.asyncio
    async def test_is_stream_flag(self, store):
        """is_stream 正确写入"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m", is_stream=1))

        span = await store.get_span(span_id)
        assert span["is_stream"] == 1

    # ── 新增: P2 升级/扩展 ──

    @pytest.mark.asyncio
    async def test_create_trace_with_session_id(self, store):
        """session_id 正确写入"""
        trace_id = str(uuid.uuid4())
        await store.create_trace(trace_id, session_id="sess-123")
        trace = await store.get_trace(trace_id)
        assert trace["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_list_traces_filter_by_status(self, store):
        """按 status 过滤"""
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        await store.create_trace(t1)
        await store.create_trace(t2)
        await store.update_trace(trace_id=t1, status="error")

        traces = await store.list_traces(status="error")
        assert len(traces) >= 1
        assert all(t["status"] == "error" for t in traces)

    @pytest.mark.asyncio
    async def test_list_traces_filter_by_agent_id(self, store):
        """按 agent_id 过滤"""
        t1 = str(uuid.uuid4())
        t2 = str(uuid.uuid4())
        await store.create_trace(t1, agent_id="agent-a")
        await store.create_trace(t2, agent_id="agent-b")

        traces = await store.list_traces(agent_id="agent-a")
        assert len(traces) >= 1
        assert traces[0]["agent_id"] == "agent-a"

    # ── 新增: P3 运维元数据 ──

    @pytest.mark.asyncio
    async def test_create_trace_with_client_info(self, store):
        """client_ip / user_agent 正确写入"""
        trace_id = str(uuid.uuid4())
        await store.create_trace(trace_id, client_ip="192.168.1.1", user_agent="TestClient/1.0")
        trace = await store.get_trace(trace_id)
        assert trace["client_ip"] == "192.168.1.1"
        assert trace["user_agent"] == "TestClient/1.0"

    @pytest.mark.asyncio
    async def test_finish_span_with_upstream_url(self, store):
        """upstream_url / gateway_version 正确写入"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))

        await store.finish_span(span_id=span_id, upstream_url="https://api.openai.com", gateway_version="2.0.0")
        span = await store.get_span(span_id)
        assert span["upstream_url"] == "https://api.openai.com"
        assert span["gateway_version"] == "2.0.0"

    # ── 新增: 聚合统计 ──

    @pytest.mark.asyncio
    async def test_get_stats_extended(self, store):
        """get_stats 返回扩展统计字段"""
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())
        await store.create_trace(trace_id)
        await store.create_span(TraceSpan(trace_id=trace_id, span_id=span_id, provider="o", model="m"))
        await store.finish_span(
            span_id=span_id,
            latency_ms=100.0,
            ttft_ms=50.0,
            estimated_cost_usd=0.01,
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        stats = await store.get_stats(hours=1)
        assert "total_requests" in stats
        assert "avg_ttft_ms" in stats
        assert "total_estimated_cost_usd" in stats
        assert "p50_latency_ms" in stats


# ==========================================================================
# TestSpanTree — 树层测试
# ==========================================================================


class TestSpanTree:
    """Tests for SpanTree builder (v2)."""

    @pytest.mark.asyncio
    async def test_empty_spans(self):
        """Should return None for empty span list."""
        tree = SpanTree([])
        assert await tree.build() is None

    @pytest.mark.asyncio
    async def test_single_span(self):
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
                "guard_hits_json": "[]",
                "eval_scores_json": "[]",
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        assert root is not None
        assert root.span_id == "span1"
        assert root.total_tokens == 15
        assert root.subtree_tokens == 15

    @pytest.mark.asyncio
    async def test_nested_spans(self):
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
                "guard_hits_json": "[]",
                "eval_scores_json": "[]",
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
                "guard_hits_json": "[]",
                "eval_scores_json": "[]",
                "created_at": "2024-01-01T00:00:01",
            },
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        assert root is not None
        assert len(root.children) == 1
        assert root.children[0].span_id == "span2"
        assert root.children[0].depth == 1
        assert root.subtree_tokens == 15 + 8
        assert root.subtree_latency_ms == 150.0

    @pytest.mark.asyncio
    async def test_to_dict(self):
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
                "guard_hits_json": json.dumps(
                    [{"rule_id": "pii-detection", "action": "block", "matches": [], "confidence": 0.9, "details": ""}]
                ),
                "eval_scores_json": json.dumps([{"name": "relevance", "score": 0.9, "details": ""}]),
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        result = SpanTree.to_dict(root)
        assert result is not None
        assert result["span_id"] == "span1"
        assert result["total_tokens"] == 15
        # guard_hits 现在序列化为 list[dict]
        assert len(result["guard_hits"]) == 1
        assert result["guard_hits"][0]["rule_id"] == "pii-detection"
        # eval_scores 现在序列化为 list[dict]
        assert len(result["eval_scores"]) == 1
        assert result["eval_scores"][0]["score"] == 0.9
        assert result["children"] == []

    @pytest.mark.asyncio
    async def test_to_dict_includes_new_fields(self):
        """to_dict() 序列化包含 P0-P3 新字段"""
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
                "ttft_ms": 50.0,
                "estimated_cost_usd": 0.01,
                "is_stream": 1,
                "request_path": "/v1/chat/completions",
                "finish_reason": "stop",
                "error_message": None,
                "temperature": 0.7,
                "max_tokens": 100,
                "tool_calls_json": json.dumps([{"id": "tc1", "name": "search", "arguments": {"q": "hi"}}]),
                "request_summary": "user: hi",
                "response_summary": "Hello!",
                "guard_hits_json": "[]",
                "eval_scores_json": "[]",
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        result = SpanTree.to_dict(root)
        assert result is not None
        assert result["is_stream"] is True
        assert result["ttft_ms"] == 50.0
        assert result["estimated_cost_usd"] == 0.01
        assert result["request_path"] == "/v1/chat/completions"
        assert result["finish_reason"] == "stop"
        assert result["temperature"] == 0.7
        assert result["max_tokens"] == 100
        assert result["request_summary"] == "user: hi"
        assert result["response_summary"] == "Hello!"
        assert result["tool_calls"] is not None
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "search"

    @pytest.mark.asyncio
    async def test_guard_hits_parsed_as_records(self):
        """guard_hits_json 反序列化为 GuardHitRecord 对象"""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0,
                "guard_hits_json": json.dumps(
                    [
                        {
                            "rule_id": "pii",
                            "action": "block",
                            "matches": ["138****"],
                            "confidence": 0.95,
                            "details": "phone number detected",
                        },
                    ]
                ),
                "eval_scores_json": "[]",
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        assert root is not None
        assert len(root.guard_hits) == 1
        hit = root.guard_hits[0]
        assert isinstance(hit, GuardHitRecord)
        assert hit.rule_id == "pii"
        assert hit.action == "block"
        assert hit.matches == ["138****"]
        assert hit.confidence == 0.95

    @pytest.mark.asyncio
    async def test_eval_scores_parsed_as_records(self):
        """eval_scores_json 反序列化为 EvalScoreRecord 对象"""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0,
                "guard_hits_json": "[]",
                "eval_scores_json": json.dumps(
                    [
                        {"name": "relevance", "score": 0.88, "details": "good"},
                        {"name": "coherence", "score": 0.92, "details": "excellent"},
                    ]
                ),
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        assert root is not None
        assert len(root.eval_scores) == 2
        assert isinstance(root.eval_scores[0], EvalScoreRecord)
        assert root.eval_scores[0].name == "relevance"
        assert root.eval_scores[0].score == 0.88

    @pytest.mark.asyncio
    async def test_tool_calls_parsed_from_json(self):
        """tool_calls_json 反序列化为 ToolCall 列表"""
        spans = [
            {
                "span_id": "span1",
                "trace_id": "trace1",
                "parent_span_id": None,
                "provider": "openai",
                "model": "gpt-4o",
                "status": "ok",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0,
                "guard_hits_json": "[]",
                "eval_scores_json": "[]",
                "tool_calls_json": json.dumps(
                    [
                        {"id": "call_1", "name": "get_weather", "arguments": {"city": "Beijing"}},
                        {"id": "call_2", "name": "search", "arguments": {"q": "news"}},
                    ]
                ),
                "created_at": "2024-01-01T00:00:00",
            }
        ]
        tree = SpanTree(spans)
        root = await tree.build()
        assert root is not None
        assert root.tool_calls is not None
        assert len(root.tool_calls) == 2
        assert root.tool_calls[0].name == "get_weather"
        assert root.tool_calls[1].arguments == {"q": "news"}


# ==========================================================================
# TestTraceEngine — 引擎层测试
# ==========================================================================


class TestTraceEngine:
    """Tests for TraceEngine lifecycle (v2)."""

    @pytest.fixture
    async def store(self, temp_db_path):
        s = TraceStore(db_path=temp_db_path)
        await s.initialize()
        yield s
        await s.close()

    @pytest.fixture
    async def engine(self, store):
        from gateway.trace.engine import TraceEngine

        return TraceEngine(store)

    @pytest.mark.asyncio
    async def test_start_span_with_new_params(self, engine, store):
        """start_span with SpanStartParams including is_stream / temperature"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {"X-Agent-ID": "agent1", "X-Session-ID": "sess1"}
        mock_req.client = None
        mock_req.url.path = "/v1/chat/completions"

        trace_id, root_span_id = await engine.start_trace(mock_req)

        params = SpanStartParams(
            provider="openai",
            model="gpt-4o",
            request_path="/v1/chat/completions",
            is_stream=True,
            temperature=0.7,
            max_tokens=256,
        )
        child_span_id = await engine.start_span(params)

        span = await store.get_span(child_span_id)
        assert span is not None
        assert span["provider"] == "openai"
        assert span["model"] == "gpt-4o"
        assert span["request_path"] == "/v1/chat/completions"
        assert span["is_stream"] == 1
        assert span["temperature"] == 0.7
        assert span["max_tokens"] == 256

    @pytest.mark.asyncio
    async def test_finish_span_content_routing_small(self, engine, store):
        """finish_span 含 ≤4KB request_body → 内联存储"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, span_id = await engine.start_trace(mock_req)

        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=span_id,
                status="ok",
                request_body={"messages": [{"role": "user", "content": "hi"}]},
                response_body={"content": "hello"},
            )
        )

        span = await store.get_span(span_id)
        assert span is not None
        # 小内容应内联
        assert span["request_body_json"] is not None
        assert span["response_body_json"] is not None
        assert span["content_id"] is None or span["content_id"] == ""

    @pytest.mark.asyncio
    async def test_finish_span_content_routing_large(self, engine, store):
        """finish_span 含 >4KB request_body → span_contents 存储"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, span_id = await engine.start_trace(mock_req)

        # 构造 >4KB 的请求体
        large_body = {"messages": [{"role": "user", "content": "x" * 5000}]}
        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=span_id,
                status="ok",
                request_body=large_body,
                response_body={"content": "ok"},
            )
        )

        span = await store.get_span(span_id)
        assert span is not None
        # 大内容应有 content_id
        assert span["content_id"] is not None
        assert span["content_id"] != ""

    @pytest.mark.asyncio
    async def test_finish_span_with_guard_hit_records(self, engine, store):
        """finish_span 传入 GuardHitRecord 列表"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, span_id = await engine.start_trace(mock_req)

        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=span_id,
                status="blocked",
                guard_hits=[
                    GuardHitRecord(
                        rule_id="pii", action="block", matches=["138****"], confidence=0.95, details="phone"
                    ),
                ],
            )
        )

        span = await store.get_span(span_id)
        assert span["status"] == "blocked"
        parsed = json.loads(span["guard_hits_json"])
        assert parsed[0]["rule_id"] == "pii"
        assert parsed[0]["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_finish_span_with_eval_score_records(self, engine, store):
        """finish_span 传入 EvalScoreRecord 列表"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, span_id = await engine.start_trace(mock_req)

        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=span_id,
                status="ok",
                eval_scores=[
                    EvalScoreRecord(name="relevance", score=0.9, details="good"),
                ],
            )
        )

        span = await store.get_span(span_id)
        parsed = json.loads(span["eval_scores_json"])
        assert parsed[0]["name"] == "relevance"
        assert parsed[0]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_finish_span_error_with_message(self, engine, store):
        """status=error 时 error_message 正确持久化"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, span_id = await engine.start_trace(mock_req)

        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=span_id,
                status="timeout",
                error_message="Upstream timed out after 30s",
            )
        )

        span = await store.get_span(span_id)
        assert span["status"] == "timeout"
        assert span["error_message"] == "Upstream timed out after 30s"

    @pytest.mark.asyncio
    async def test_trace_level_cost_aggregation(self, engine, store):
        """多个 span 的 estimated_cost_usd 正确聚合到 trace"""
        from unittest.mock import AsyncMock

        from fastapi import Request

        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {}
        mock_req.client = None
        mock_req.url.path = "/v1/chat"

        trace_id, root_id = await engine.start_trace(mock_req)

        # 第一个 span: gpt-4o 调用
        params1 = SpanStartParams(provider="openai", model="gpt-4o", is_stream=False)
        s1 = await engine.start_span(params1)
        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=s1,
                token_usage=TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
            )
        )
        span1 = await store.get_span(s1)
        cost1 = float(span1.get("estimated_cost_usd", 0))

        # 第二个 span: 另一个调用
        s2 = await engine.start_span(params1)
        await engine.finish_span(
            SpanFinishParams(
                trace_id=trace_id,
                span_id=s2,
                token_usage=TokenUsage(prompt_tokens=500, completion_tokens=200, total_tokens=700),
            )
        )
        span2 = await store.get_span(s2)
        cost2 = float(span2.get("estimated_cost_usd", 0))

        assert cost1 > 0
        assert cost2 > 0

        # trace 级别聚合
        trace = await engine.get_trace(trace_id)
        assert trace is not None
        total_cost = float(trace.get("estimated_cost_usd", 0))
        assert total_cost == pytest.approx(cost1 + cost2, rel=1e-6)

    @pytest.mark.asyncio
    async def test_start_trace_extracts_client_info(self, engine, store):
        """start_trace 从 Request 提取 session_id / client_ip / user_agent"""
        from unittest.mock import AsyncMock, MagicMock

        from fastapi import Request

        mock_client = MagicMock()
        mock_client.host = "10.0.0.1"
        mock_req = AsyncMock(spec=Request)
        mock_req.headers = {
            "X-Agent-ID": "agent-x",
            "X-Session-ID": "sess-abc",
            "User-Agent": "openai-python/1.0",
        }
        mock_req.client = mock_client
        mock_req.url.path = "/v1/chat/completions"

        trace_id, span_id = await engine.start_trace(mock_req)
        trace = await store.get_trace(trace_id)

        assert trace is not None
        assert trace["agent_id"] == "agent-x"
        assert trace["session_id"] == "sess-abc"
        assert trace["client_ip"] == "10.0.0.1"
        assert trace["user_agent"] == "openai-python/1.0"
