"""TraceEngine — generates trace/span IDs, manages span lifecycle, and stores trace data.

v2 — extended with content tiered storage, cost estimation, TTFT, and structured guard/eval records.
"""

from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar
from typing import Any, cast

from fastapi import Request

from shared.logging import get_logger
from shared.models import (
    EvalScoreRecord,
    GuardHitRecord,
    SpanFinishParams,
    SpanStartParams,
    TokenUsage,
    TraceSpan,
)

from .pricing import estimate_cost
from .store import TraceStore
from .tree import SpanTree

logger = get_logger()

# Context variables for async-safe trace/span context propagation
_current_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_current_span_id: ContextVar[str | None] = ContextVar("span_id", default=None)
_current_agent_id: ContextVar[str | None] = ContextVar("agent_id", default=None)
_current_request_path: ContextVar[str] = ContextVar("request_path", default="")

# 内容分级存储阈值（字节）
INLINE_THRESHOLD = 4096


class TraceEngine:
    """Generates and manages traces and spans for the gateway (v2).

    Each incoming request gets a unique trace_id and span_id.
    Nested calls (e.g., Agent → LLM → Tool → LLM) form a span tree
    connected via parent_span_id.

    Uses contextvars for async-safe context propagation.
    Supports content tiered storage, cost estimation, and structured guard/eval.
    """

    def __init__(self, store: TraceStore) -> None:
        """Args:
            store: 已调用 initialize() 的 TraceStore 实例.
        """
        self._store = store
        self._span_start_times: dict[str, float] = {}

    @property
    def store(self) -> TraceStore:
        """Returns: the bound TraceStore instance."""
        return self._store

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    @staticmethod
    def get_current_trace_id() -> str | None:
        return _current_trace_id.get()

    @staticmethod
    def get_current_span_id() -> str | None:
        return _current_span_id.get()

    @staticmethod
    def set_context(
        trace_id: str, span_id: str, agent_id: str | None = None
    ) -> None:
        _current_trace_id.set(trace_id)
        _current_span_id.set(span_id)
        if agent_id is not None:
            _current_agent_id.set(agent_id)

    @staticmethod
    def clear_context() -> None:
        _current_trace_id.set(None)
        _current_span_id.set(None)
        _current_agent_id.set(None)
        _current_request_path.set("")

    # ------------------------------------------------------------------
    # Trace 生命周期
    # ------------------------------------------------------------------

    async def start_trace(self, request: Request) -> tuple[str, str]:
        """为一次外部请求创建 root trace + root span。

        从 FastAPI Request 中提取:
        - agent_id:     request.headers.get("X-Agent-ID")
        - session_id:   request.headers.get("X-Session-ID")
        - client_ip:    request.client.host if request.client else None
        - user_agent:   request.headers.get("User-Agent")
        - request_path: request.url.path

        Returns:
            (trace_id, span_id) — 均为 UUID v4 字符串.

        Raises:
            aiosqlite.Error — 数据库写入失败.
        """
        trace_id = str(uuid.uuid4())
        span_id = str(uuid.uuid4())

        agent_id = request.headers.get("X-Agent-ID")
        session_id = request.headers.get("X-Session-ID")
        client_ip = request.client.host if request.client else None
        user_agent = request.headers.get("User-Agent")
        request_path = request.url.path

        _current_agent_id.set(agent_id)
        _current_request_path.set(request_path)
        self.set_context(trace_id, span_id, agent_id)
        self._span_start_times[span_id] = time.monotonic()

        await self._store.create_trace(
            trace_id,
            agent_id=agent_id,
            session_id=session_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

        # Create root span record
        root_span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            provider="gateway",
            model="",
            request_path=request_path,
            is_stream=0,  # root span 承载 request 级别信息
        )
        await self._store.create_span(root_span)

        logger.debug(
            "trace_started",
            trace_id=trace_id,
            span_id=span_id,
            agent_id=agent_id,
            session_id=session_id,
            path=request_path,
        )
        return trace_id, span_id

    # ------------------------------------------------------------------
    # Span 生命周期
    # ------------------------------------------------------------------

    async def start_span(self, params: SpanStartParams) -> str:
        """为嵌套调用（如 tool 执行）创建子 span。

        Precondition: start_trace() 已调用，存在活跃 trace.

        Returns:
            新创建的 span_id (UUID v4).

        Raises:
            RuntimeError: 没有活跃 trace.
            aiosqlite.Error: 数据库写入失败.
        """
        trace_id = self.get_current_trace_id()
        if trace_id is None:
            raise RuntimeError("No active trace — call start_trace() first")

        span_id = str(uuid.uuid4())
        parent_id = params.parent_span_id or self.get_current_span_id()
        request_path = params.request_path or _current_request_path.get()

        self.set_context(trace_id, span_id)
        self._span_start_times[span_id] = time.monotonic()

        span = TraceSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_id,
            provider=params.provider,
            model=params.model,
            request_hash=params.request_hash,
            request_path=request_path,
            is_stream=1 if params.is_stream else 0,
            temperature=params.temperature,
            max_tokens=params.max_tokens,
        )
        await self._store.create_span(span)
        return span_id

    async def finish_span(self, params: SpanFinishParams) -> None:
        """完成一个 span 并持久化所有最终数据。

        内部处理流程:
        1. 计算 latency_ms
        2. 内容分级存储（≤4KB 内联 / >4KB 入库 span_contents）
        3. 生成摘要
        4. JSON 序列化 guard_hits / eval_scores
        5. 费用计算
        6. 调用 store 持久化
        7. 聚合 trace 统计

        Precondition:
            params.status ∈ {"ok", "error", "timeout", "blocked"}
            status ∈ {"error", "timeout"} 时 error_message 应为非空字符串.

        Raises:
            aiosqlite.Error — 内部捕获并记录日志，不向上传播（避免阻塞 HTTP 响应）.
        """
        latency_ms = self._compute_latency(params.span_id)

        # --- 序列化 guard_hits / eval_scores ---
        guard_hits_json: str | None = None
        if params.guard_hits is not None:
            guard_hits_raw = [g.model_dump() for g in params.guard_hits]
            guard_hits_json = json.dumps(guard_hits_raw, ensure_ascii=False)

        eval_scores_json: str | None = None
        if params.eval_scores is not None:
            eval_scores_raw = [e.model_dump() for e in params.eval_scores]
            eval_scores_json = json.dumps(eval_scores_raw, ensure_ascii=False)

        # --- Tool calls 序列化 ---
        tool_calls_json: str | None = None
        if params.tool_calls is not None:
            tool_calls_raw = [tc.model_dump() for tc in params.tool_calls]
            tool_calls_json = json.dumps(tool_calls_raw, ensure_ascii=False)
        elif params.tool_calls_json is not None:
            tool_calls_json = params.tool_calls_json

        # --- 内容序列化 + 分级存储 ---
        request_json: str | None = None
        response_json: str | None = None
        if params.request_body is not None:
            request_json = json.dumps(params.request_body, ensure_ascii=False)
        if params.response_body is not None:
            response_json = json.dumps(params.response_body, ensure_ascii=False)

        content_id: str | None = None
        request_body_inline: str | None = None
        response_body_inline: str | None = None

        if request_json is not None or response_json is not None:
            req = request_json or ""
            resp = response_json or ""
            if self._should_store_inline(req, resp):
                request_body_inline = request_json
                response_body_inline = response_json
            else:
                content_id = str(uuid.uuid4())
                try:
                    await self._store_large_content(
                        content_id, params.span_id, req, resp
                    )
                except Exception as exc:
                    logger.error(
                        "store_large_content_failed",
                        span_id=params.span_id,
                        error=str(exc),
                    )
                    content_id = None
                    # fallback: 截断后内联存储
                    request_body_inline = _truncate_str(req, INLINE_THRESHOLD)
                    response_body_inline = _truncate_str(resp, INLINE_THRESHOLD)

        # --- 生成摘要 ---
        request_summary = _generate_summary(request_json, 200) if request_json else None
        response_summary = (
            _generate_summary(response_json, 500) if response_json else None
        )

        # --- 费用计算 ---
        cost = params.estimated_cost_usd
        if cost == 0.0 and params.token_usage is not None:
            # 从 SpanStartParams 中获取 model（需要从 context 中推断）
            # 这里使用 trace 上下文中的信息
            model = _current_request_path.get("") or ""  # fallback
            # Actually, model is stored in the span during start_span.
            # We need to extract it. For now, use a simplified approach:
            # Re-read the span to get its model
            try:
                span_data = await self._store.get_span(params.span_id)
                if span_data:
                    model = str(span_data.get("model", ""))
                    if model:
                        cost = estimate_cost(
                            model,
                            params.token_usage.prompt_tokens,
                            params.token_usage.completion_tokens,
                        )
            except Exception:
                cost = 0.0

        # --- 持久化到 store ---
        try:
            await self._store.finish_span(
                span_id=params.span_id,
                status=params.status,
                token_usage=params.token_usage,
                latency_ms=latency_ms,
                ttft_ms=params.ttft_ms,
                estimated_cost_usd=cost,
                finish_reason=params.finish_reason,
                error_message=params.error_message,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                tool_calls_json=tool_calls_json,
                guard_hits_json=guard_hits_json,
                eval_scores_json=eval_scores_json,
                request_summary=request_summary,
                response_summary=response_summary,
                content_id=content_id,
                request_body_json=request_body_inline,
                response_body_json=response_body_inline,
                upstream_url=params.upstream_url,
                gateway_version=params.gateway_version,
            )

            # 聚合 trace 统计
            await self._update_trace_stats(params.trace_id)
        except Exception as exc:
            self._store.write_failures += 1
            logger.error(
                "finish_span_persist_failed",
                trace_id=params.trace_id,
                span_id=params.span_id,
                error=str(exc),
                total_failures=self._store.write_failures,
            )
            return

        logger.debug(
            "span_finished",
            trace_id=params.trace_id,
            span_id=params.span_id,
            status=params.status,
            latency_ms=round(latency_ms, 1),
            cost=round(cost, 6),
        )

    # ------------------------------------------------------------------
    # 僵尸 span 清理
    # ------------------------------------------------------------------

    async def cleanup_abandoned_spans(self, abandoned_minutes: int = 5) -> int:
        """Mark spans as abandoned that were started but never finished.

        Detection heuristic: spans where ``latency_ms = 0`` and
        ``status = \"ok\"`` and ``created_at`` is older than *abandoned_minutes*
        ago are virtually certain to be abandoned — they were INSERTed by
        ``start_trace`` but never reached ``finish_span``.

        Args:
            abandoned_minutes: 超出多长时间未完成则视为废弃。

        Returns:
            被标记为 abandoned 的 span 数量。
        """
        marked = await self._store.mark_abandoned_spans(
            abandoned_minutes=abandoned_minutes,
        )
        if marked > 0:
            logger.warning(
                "abandoned_spans_cleaned",
                count=marked,
                threshold_minutes=abandoned_minutes,
            )
        return marked

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    async def get_trace(self, trace_id: str) -> dict[str, object] | None:
        """查询 trace 元数据."""
        return await self._store.get_trace(trace_id)

    async def list_traces(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict[str, object]]:
        """分页查询 trace 列表（按 created_at DESC）。不加载大体积内容."""
        return await self._store.list_traces(limit=limit, offset=offset)

    async def get_span_tree(
        self, trace_id: str, load_content: bool = False
    ) -> dict[str, object] | None:
        """获取完整 span 调用树。

        Args:
            trace_id: trace ID.
            load_content: True 时对有 content_id 的 span 加载完整请求/响应.

        Returns:
            嵌套 dict（SpanTree.to_dict 输出）或 None.
        """
        spans = await self._store.get_spans(trace_id)
        if not spans:
            return None
        tree = SpanTree(spans)
        store = self._store if load_content else None
        root = tree.build(store=store)
        return SpanTree.to_dict(root)

    async def get_stats(self, hours: int = 24) -> dict[str, object]:
        """获取聚合统计（含 P50/P95/P99/avg_ttft/total_cost）."""
        return await self._store.get_stats(hours=hours)

    async def get_service_quality_stats(self, hours: int = 24) -> dict[str, object]:
        """获取服务质量聚合指标（TTFT/TPS/空响应率/流式终端率/重复率）."""
        return await self._store.get_service_quality_stats(hours=hours)

    async def sample_spans(
        self,
        hours: int = 24,
        limit: int = 50,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """按条件采样 span 记录."""
        return await self._store.sample_spans(hours=hours, limit=limit, filters=filters)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _compute_latency(self, span_id: str) -> float:
        """计算 span 耗时（ms）."""
        start = self._span_start_times.pop(span_id, None)
        if start is None:
            return 0.0
        return (time.monotonic() - start) * 1000.0

    async def _update_trace_stats(self, trace_id: str) -> None:
        """聚合所有 span 的 stats 到 trace 记录."""
        spans = await self._store.get_spans(trace_id)
        if not spans:
            return

        total_tokens = 0
        total_latency = 0.0
        total_cost = 0.0
        final_status = "ok"
        status_priority = {
            "error": 0,
            "timeout": 1,
            "abandoned": 2,
            "blocked": 3,
            "rate_limited": 4,
            "ok": 5,
        }

        for s in spans:
            total_tokens += int(cast(int, s.get("prompt_tokens", 0))) + int(
                cast(int, s.get("completion_tokens", 0))
            )
            total_latency += float(cast(float, s.get("latency_ms", 0)))
            total_cost += float(cast(float, s.get("estimated_cost_usd", 0)))

            s_status = cast(str, s.get("status", "ok"))
            if status_priority.get(s_status, 5) < status_priority.get(final_status, 5):
                final_status = s_status

        await self._store.update_trace(
            trace_id=trace_id,
            total_tokens=total_tokens,
            total_latency_ms=total_latency,
            estimated_cost_usd=total_cost,
            status=final_status,
        )

    @staticmethod
    def _should_store_inline(request_json: str, response_json: str) -> bool:
        """判断内容是否应内联存储."""
        return len(request_json) + len(response_json) <= INLINE_THRESHOLD

    async def _store_large_content(
        self,
        content_id: str,
        span_id: str,
        request_json: str,
        response_json: str,
    ) -> None:
        """写入 span_contents 表."""
        await self._store.insert_span_content(
            content_id=content_id,
            span_id=span_id,
            request_body=request_json,
            response_body=response_json,
        )


# ------------------------------------------------------------------
# 模块级工具函数
# ------------------------------------------------------------------

def _generate_summary(body_json: str | None, max_chars: int) -> str | None:
    """生成内容摘要。

    Args:
        body_json: JSON 序列化后的内容字符串.
        max_chars: 最大字符数.

    Returns:
        截取后的摘要字符串，或 None.
    """
    if not body_json:
        return None
    if len(body_json) <= max_chars:
        return body_json
    return body_json[:max_chars]


def _truncate_str(text: str, max_chars: int) -> str:
    """截断字符串到指定字符数."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]
