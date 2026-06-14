"""SpanTree — build and serialize trace span trees from flat span records.

Schema v2 — extended with P0-P3 fields, structured guard_hits/eval_scores,
and optional span_contents loading.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict, cast

from shared.models import EvalScoreRecord, GuardHitRecord, ToolCall

if TYPE_CHECKING:
    from .store import TraceStore


# ---------------------------------------------------------------------------
# TypedDict: 从 SQLite 读出的 span 行格式
# ---------------------------------------------------------------------------

class _SpanRowBase(TypedDict):
    """Required fields — every span must have at least a span_id."""

    span_id: str


class _SpanRow(_SpanRowBase, total=False):
    """Typed view of a span row coming from the SQLite store (v2 schema)."""

    trace_id: str
    parent_span_id: str | None
    provider: str
    model: str | None
    request_hash: str
    request_path: str
    is_stream: int
    status: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    ttft_ms: float
    estimated_cost_usd: float
    finish_reason: str | None
    error_message: str | None
    temperature: float | None
    max_tokens: int | None
    tool_calls_json: str | None
    content_id: str | None
    request_summary: str | None
    response_summary: str | None
    request_body_json: str | None
    response_body_json: str | None
    guard_hits_json: str
    eval_scores_json: str
    upstream_url: str | None
    gateway_version: str | None
    created_at: str
    # deprecated field — still may exist in rows from old schema
    guard_hits: str | list[str]
    eval_scores: str | dict[str, float]


# ---------------------------------------------------------------------------
# SpanNode
# ---------------------------------------------------------------------------

@dataclass
class SpanNode:
    """A node in the span tree, enriched with computed statistics (v2)."""

    # ── 基础字段 ──
    span_id: str
    trace_id: str
    parent_span_id: str | None
    provider: str
    model: str | None
    status: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    created_at: str

    # ── 结构化 guard/eval（升级 P2） ──
    guard_hits: list[GuardHitRecord]
    eval_scores: list[EvalScoreRecord]

    # ── P0 内容上下文 ──
    finish_reason: str | None = None
    error_message: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tool_calls: list[ToolCall] | None = None
    request_summary: str | None = None
    response_summary: str | None = None
    content_id: str | None = None
    request_body: str | None = None       # 从 span_contents 加载的 JSON 字符串
    response_body: str | None = None      # 从 span_contents 加载的 JSON 字符串

    # ── P1 成本与性能 ──
    is_stream: bool = False
    ttft_ms: float = 0.0
    estimated_cost_usd: float = 0.0

    # ── P2 增强分析 ──
    request_path: str = ""

    # ── P3 运维元数据 ──
    upstream_url: str | None = None
    gateway_version: str | None = None

    # ── 计算字段（不变） ──
    children: list[SpanNode] = field(default_factory=list)
    subtree_tokens: int = 0
    subtree_latency_ms: float = 0.0
    depth: int = 0


# ---------------------------------------------------------------------------
# SpanTree
# ---------------------------------------------------------------------------

class SpanTree:
    """Build a tree of spans from flat span records and compute statistics.

    Supports:
    - v2 schema with structured GuardHitRecord / EvalScoreRecord
    - Backward-compatible parsing of old guard_hits: list[str] format
    - Optional span_contents loading for large content
    """

    def __init__(
        self,
        spans: "Iterable[Mapping[str, object]]",
    ) -> None:
        # Cast through `object` to satisfy strict type checker.
        self._spans: list[_SpanRow] = [cast(_SpanRow, cast(object, s)) for s in spans]

    async def build(
        self,
        store: "TraceStore | None" = None,
    ) -> SpanNode | None:
        """Build the span tree and return the root node.

        Args:
            store: 可选 TraceStore，用于加载 span_contents 大内容。
                   不传则 span_node.request_body/response_body 为 None。

        Returns:
            Root SpanNode or None if spans list is empty.
        """
        import asyncio

        if not self._spans:
            return None

        # Parse all spans into nodes
        nodes: dict[str, SpanNode] = {}
        content_ids: list[str] = []

        for span in self._spans:
            guard_hits = self._parse_guard_hits(span)
            eval_scores = self._parse_eval_scores(span)
            tool_calls = self._parse_tool_calls(span)

            cid = span.get("content_id")
            if cid and isinstance(cid, str):
                content_ids.append(cid)

            nodes[span["span_id"]] = SpanNode(
                span_id=span["span_id"],
                trace_id=span.get("trace_id", ""),
                parent_span_id=span.get("parent_span_id"),
                provider=span.get("provider", ""),
                model=span.get("model"),
                status=span.get("status", "ok"),
                latency_ms=float(span.get("latency_ms", 0)),
                prompt_tokens=int(span.get("prompt_tokens", 0)),
                completion_tokens=int(span.get("completion_tokens", 0)),
                total_tokens=int(span.get("prompt_tokens", 0))
                + int(span.get("completion_tokens", 0)),
                created_at=str(span.get("created_at", "")),
                # 结构化记录
                guard_hits=guard_hits,
                eval_scores=eval_scores,
                tool_calls=tool_calls,
                # P0
                finish_reason=span.get("finish_reason"),
                error_message=span.get("error_message"),
                temperature=(
                    float(t) if (t := span.get("temperature")) is not None
                    else None
                ),
                max_tokens=span.get("max_tokens"),
                request_summary=span.get("request_summary"),
                response_summary=span.get("response_summary"),
                content_id=cid,
                # P1
                is_stream=bool(span.get("is_stream", 0)),
                ttft_ms=float(span.get("ttft_ms", 0)),
                estimated_cost_usd=float(span.get("estimated_cost_usd", 0)),
                # P2
                request_path=span.get("request_path", ""),
                # P3
                upstream_url=span.get("upstream_url"),
                gateway_version=span.get("gateway_version"),
            )

        # Build the tree structure
        root = None
        for node in nodes.values():
            if node.parent_span_id and node.parent_span_id in nodes:
                parent = nodes[node.parent_span_id]
                parent.children.append(node)
            else:
                root = node

        # If no clear root, pick the one with no parent
        if root is None:
            for node in nodes.values():
                if node.parent_span_id is None or node.parent_span_id not in nodes:
                    root = node
                    break

        # Compute depth and subtree statistics
        if root:
            self._compute_tree(root, depth=0)

        # Load span_contents if store is provided
        if store is not None and content_ids:
            await self._load_span_contents(nodes, content_ids, store)

        return root

    async def _load_span_contents(
        self,
        nodes: dict[str, SpanNode],
        content_ids: list[str],
        store: "TraceStore",
    ) -> None:
        """Asynchronously load span_contents for given content_ids.

        Called from the async build() method — safe within the running event loop.
        """
        from collections.abc import Awaitable

        tasks: list[Awaitable[object]] = [
            store.get_span_content(cid) for cid in content_ids
        ]
        results = await asyncio.gather(*tasks)

        for cid, result in zip(content_ids, results):
            if result is None or not isinstance(result, dict):
                continue
            for node in nodes.values():
                if node.content_id == cid:
                    rb = result.get("request_body")
                    resp_b = result.get("response_body")
                    node.request_body = rb if isinstance(rb, str) else None
                    node.response_body = resp_b if isinstance(resp_b, str) else None
                    break

    def _compute_tree(self, node: SpanNode, depth: int) -> None:
        """Recursively compute depth and aggregate subtree statistics."""
        node.depth = depth
        node.subtree_tokens = node.total_tokens
        node.subtree_latency_ms = node.latency_ms

        for child in node.children:
            self._compute_tree(child, depth + 1)
            node.subtree_tokens += child.subtree_tokens
            node.subtree_latency_ms += child.subtree_latency_ms

    # ------------------------------------------------------------------
    # JSON 解析（私有方法）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_guard_hits(span: _SpanRow) -> list[GuardHitRecord]:
        """解析 guard_hits，兼容新旧格式。

        - 新格式: guard_hits_json = '[{"rule_id":...,"action":...}]'
        - 旧格式: guard_hits = '["rule1","rule2"]'
        """
        # 优先从新字段读取
        raw = span.get("guard_hits_json")
        if raw and isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list) and parsed:
                # 新格式: list[dict] → list[GuardHitRecord]
                if isinstance(parsed[0], dict):
                    return [
                        GuardHitRecord(
                            rule_id=r.get("rule_id", ""),
                            action=r.get("action", ""),
                            matches=r.get("matches", []),
                            confidence=r.get("confidence", 0.0),
                            details=r.get("details", ""),
                        )
                        for r in parsed
                    ]
                # 旧格式遗留在新字段中: list[str] → 创建最小 GuardHitRecord
                if isinstance(parsed[0], str):
                    return [
                        GuardHitRecord(rule_id=r, action="", matches=[], confidence=0.0, details="")
                        for r in parsed
                    ]

        # Fallback: 旧字段 guard_hits
        raw_old = span.get("guard_hits")
        if isinstance(raw_old, str):
            try:
                parsed_old = json.loads(raw_old)
            except (json.JSONDecodeError, TypeError):
                parsed_old = []
            if isinstance(parsed_old, list):
                return [
                    GuardHitRecord(
                        rule_id=r if isinstance(r, str) else str(r),
                        action="",
                        matches=[],
                        confidence=0.0,
                        details="",
                    )
                    for r in parsed_old
                ]
            return []
        if isinstance(raw_old, list):
            return [
                GuardHitRecord(
                    rule_id=r if isinstance(r, str) else str(r),
                    action="",
                    matches=[],
                    confidence=0.0,
                    details="",
                )
                for r in raw_old
            ]
        return []

    @staticmethod
    def _parse_eval_scores(span: _SpanRow) -> list[EvalScoreRecord]:
        """解析 eval_scores，兼容新旧格式。

        - 新格式: eval_scores_json = '[{"name":"relevance","score":0.9,"details":""}]'
        - 旧格式: eval_scores = '{"relevance":0.9}'
        """
        # 优先从新字段读取
        raw = span.get("eval_scores_json")
        if raw and isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, list):
                return [
                    EvalScoreRecord(
                        name=r.get("name", ""),
                        score=r.get("score", 0.0),
                        details=r.get("details", ""),
                    )
                    for r in parsed
                ]
            if isinstance(parsed, dict):
                return [
                    EvalScoreRecord(name=k, score=v, details="")
                    for k, v in parsed.items()
                    if isinstance(v, (int, float))
                ]

        # Fallback: 旧字段 eval_scores
        raw_old = span.get("eval_scores")
        if isinstance(raw_old, str):
            try:
                parsed_old = json.loads(raw_old)
            except (json.JSONDecodeError, TypeError):
                parsed_old = {}
            if isinstance(parsed_old, dict):
                return [
                    EvalScoreRecord(name=k, score=v, details="")
                    for k, v in parsed_old.items()
                    if isinstance(v, (int, float))
                ]
            return []
        if isinstance(raw_old, dict):
            return [
                EvalScoreRecord(name=k, score=v, details="")
                for k, v in raw_old.items()
                if isinstance(v, (int, float))
            ]
        return []

    @staticmethod
    def _parse_tool_calls(span: _SpanRow) -> list[ToolCall] | None:
        """解析 tool_calls_json → list[ToolCall]."""
        raw = span.get("tool_calls_json")
        if not raw or not isinstance(raw, str):
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, list):
            return None
        try:
            return [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                )
                for tc in parsed
            ]
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @staticmethod
    def to_dict(node: SpanNode | None) -> dict[str, object] | None:
        """Serialize a span node tree to nested dict for JSON output (v2).

        Includes all P0-P3 extension fields.
        GuardHitRecord / EvalScoreRecord are serialized as list[dict].
        """
        if node is None:
            return None

        # 序列化 guard_hits 为 list[dict]
        guard_hits_serialized: list[dict[str, object]] = [
            {
                "rule_id": g.rule_id,
                "action": g.action,
                "matches": g.matches,
                "confidence": g.confidence,
                "details": g.details,
            }
            for g in node.guard_hits
        ]

        # 序列化 eval_scores 为 list[dict]
        eval_scores_serialized: list[dict[str, object]] = [
            {
                "name": e.name,
                "score": e.score,
                "details": e.details,
            }
            for e in node.eval_scores
        ]

        # 序列化 tool_calls 为 list[dict]
        tool_calls_serialized: list[dict[str, object]] | None = None
        if node.tool_calls:
            tool_calls_serialized = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                }
                for tc in node.tool_calls
            ]

        return {
            "span_id": node.span_id,
            "trace_id": node.trace_id,
            "parent_span_id": node.parent_span_id,
            "provider": node.provider,
            "model": node.model,
            "status": node.status,
            "latency_ms": node.latency_ms,
            "prompt_tokens": node.prompt_tokens,
            "completion_tokens": node.completion_tokens,
            "total_tokens": node.total_tokens,
            "created_at": node.created_at,
            # 结构化记录
            "guard_hits": guard_hits_serialized,
            "eval_scores": eval_scores_serialized,
            # P0
            "finish_reason": node.finish_reason,
            "error_message": node.error_message,
            "temperature": node.temperature,
            "max_tokens": node.max_tokens,
            "tool_calls": tool_calls_serialized,
            "request_summary": node.request_summary,
            "response_summary": node.response_summary,
            "content_id": node.content_id,
            "request_body": (
                _truncate_large(node.request_body, 10240)
                if node.request_body
                else None
            ),
            "response_body": (
                _truncate_large(node.response_body, 20480)
                if node.response_body
                else None
            ),
            # P1
            "is_stream": node.is_stream,
            "ttft_ms": node.ttft_ms,
            "estimated_cost_usd": node.estimated_cost_usd,
            # P2
            "request_path": node.request_path,
            # P3
            "upstream_url": node.upstream_url,
            "gateway_version": node.gateway_version,
            # 计算字段
            "depth": node.depth,
            "subtree_tokens": node.subtree_tokens,
            "subtree_latency_ms": node.subtree_latency_ms,
            "children": [SpanTree.to_dict(c) for c in node.children],
        }


def _truncate_large(text: str, max_chars: int) -> str:
    """截断过大的文本，避免 API 响应过大。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... [truncated at {max_chars} chars, total {len(text)}]"
