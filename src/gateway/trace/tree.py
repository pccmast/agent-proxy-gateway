"""SpanTree — build and serialize trace span trees from flat span records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypedDict, cast


class _SpanRowBase(TypedDict):
    """Required fields — every span must have at least a span_id."""

    span_id: str


class _SpanRow(_SpanRowBase, total=False):
    """Typed view of a span row coming from the SQLite store.

    All non-base fields are optional (`total=False`) because the store
    returns `dict[str, object]` from aiosqlite and we want to be
    defensive against missing columns / nullable fields.
    """

    trace_id: str
    parent_span_id: str | None
    provider: str
    model: str | None
    status: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    guard_hits: str
    eval_scores: str
    created_at: str


@dataclass
class SpanNode:
    """A node in the span tree, enriched with computed statistics."""

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
    guard_hits: list[str]
    eval_scores: dict[str, float]
    created_at: str

    # Computed
    children: list[SpanNode] = field(default_factory=list)
    subtree_tokens: int = 0
    subtree_latency_ms: float = 0.0
    depth: int = 0


class SpanTree:
    """Build a tree of spans from flat span records and compute statistics."""

    def __init__(
        self,
        spans: "Iterable[Mapping[str, object]]",
    ) -> None:
        # Cast through `object` to satisfy strict type checker (avoids
        # "incomplete overlap" error between dict[str, object] and TypedDict).
        self._spans: list[_SpanRow] = [cast(_SpanRow, cast(object, s)) for s in spans]

    def build(self) -> SpanNode | None:
        """Build the span tree and return the root node."""
        if not self._spans:
            return None

        # Parse all spans into nodes
        nodes: dict[str, SpanNode] = {}
        for span in self._spans:
            guard_hits_raw = span.get("guard_hits", "[]")
            if isinstance(guard_hits_raw, str):
                import json
                try:
                    guard_hits = json.loads(guard_hits_raw)
                except (json.JSONDecodeError, TypeError):
                    guard_hits = []
            else:
                guard_hits = guard_hits_raw or []

            eval_scores_raw = span.get("eval_scores", "{}")
            if isinstance(eval_scores_raw, str):
                import json
                try:
                    eval_scores = json.loads(eval_scores_raw)
                except (json.JSONDecodeError, TypeError):
                    eval_scores = {}
            else:
                eval_scores = eval_scores_raw or {}

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
                total_tokens=int(span.get("prompt_tokens", 0)) + int(span.get("completion_tokens", 0)),
                guard_hits=guard_hits if isinstance(guard_hits, list) else [],
                eval_scores=eval_scores if isinstance(eval_scores, dict) else {},
                created_at=str(span.get("created_at", "")),
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

        return root

    def _compute_tree(self, node: SpanNode, depth: int) -> None:
        """Recursively compute depth and aggregate subtree statistics."""
        node.depth = depth
        node.subtree_tokens = node.total_tokens
        node.subtree_latency_ms = node.latency_ms

        for child in node.children:
            self._compute_tree(child, depth + 1)
            node.subtree_tokens += child.subtree_tokens
            node.subtree_latency_ms += child.subtree_latency_ms

    @staticmethod
    def to_dict(node: SpanNode | None) -> dict[str, object] | None:
        """Serialize a span node tree to nested dict for JSON output."""
        if node is None:
            return None
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
            "guard_hits": node.guard_hits,
            "eval_scores": node.eval_scores,
            "created_at": node.created_at,
            "depth": node.depth,
            "subtree_tokens": node.subtree_tokens,
            "subtree_latency_ms": node.subtree_latency_ms,
            "children": [SpanTree.to_dict(c) for c in node.children],
        }
