"""Agent Proxy Gateway — shared Pydantic models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class GuardAction(StrEnum):
    BLOCK = "block"
    REDACT = "redact"
    LOG = "log"


class Message(BaseModel):
    role: str
    content: str
    name: str | None = None


class ToolDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, object] | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, object]


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class NormalizedRequest(BaseModel):
    """Unified request format across all providers."""

    provider: str
    model: str
    messages: list[Message]
    tools: list[ToolDef] | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    raw_body: dict[str, object] = Field(default_factory=dict)


class NormalizedResponse(BaseModel):
    """Unified response format across all providers."""

    provider: str
    model: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    raw_body: dict[str, object] = Field(default_factory=dict)


class StreamChunk(BaseModel):
    """A single SSE chunk in the stream."""

    delta_content: str | None = None
    delta_tool_call: dict[str, object] | None = None
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    is_done: bool = False
    raw_data: dict[str, object] = Field(default_factory=dict)


class GuardResult(BaseModel):
    """Result of a guardrail check."""

    rule_id: str
    action: GuardAction
    matches: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    details: str = ""
    metadata: dict[str, object] = Field(default_factory=dict)


class EvalResult(BaseModel):
    """Result of an evaluation check."""

    name: str
    score: float  # 0-1
    details: str = ""


class EvalMetrics(BaseModel):
    """Aggregated eval scores for a span."""

    relevance: float | None = None
    safety: float | None = None
    coherence: float | None = None
    repetition_score: float = 1.0
    length_score: float = 1.0
    latency_score: float = 1.0


# ---------------------------------------------------------------------------
# Trace 系统升级 — 新增结构化记录模型
# ---------------------------------------------------------------------------


class GuardHitRecord(BaseModel):
    """安全规则命中记录（结构化），替代 guard_hits: list[str].

    Precondition: 调用方已通过 GuardResult 完成安全检测，
                  将 GuardResult 的字段映射到此模型。
    Postcondition: 序列化为 JSON 后写入数据库 guard_hits 字段。
    """

    rule_id: str
    action: str = ""  # "block" | "redact" | "log"
    matches: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    details: str = ""


class EvalScoreRecord(BaseModel):
    """评估得分记录（结构化），替代 eval_scores: dict[str, float].

    Precondition: 调用方已通过 EvalResult 完成评估。
    Postcondition: 序列化为 JSON 后写入数据库 eval_scores 字段。
    """

    name: str
    score: float  # 0-1
    details: str = ""


class SpanContent(BaseModel):
    """Span 的大体积请求/响应内容（独立存储）。

    Precondition: 请求/响应 JSON 序列化后超过 4096 字节时创建。
    Postcondition: 写入 span_contents 表，span 表通过 content_id 引用。
    """

    content_id: str
    span_id: str
    request_body: str = ""
    response_body: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class SpanStartParams(BaseModel):
    """start_span 的参数封装对象（参数 >5 个 → 必须封装）。

    Precondition: 调用方已通过 start_trace 创建了活跃 trace。
    Postcondition: TraceEngine 将此对象展开写入 TraceSpan 并调用 store.create_span()。
    """

    provider: str
    model: str
    parent_span_id: str | None = None
    request_hash: str = ""
    request_path: str = ""  # URL 路径，如 "/v1/chat/completions"
    is_stream: bool = False  # 是否流式模式
    temperature: float | None = None  # 采样温度
    max_tokens: int | None = None  # 最大输出 token 数


class SpanFinishParams(BaseModel):
    """finish_span 的参数封装对象（参数 >5 个 → 必须封装）。

    Precondition: span 已通过 start_span / start_trace 创建。
    Postcondition: engine 内部完成内容分级存储、摘要生成、费用计算、
                   聚合更新后调用 store 持久化。
    """

    trace_id: str
    span_id: str
    status: str = "ok"  # ok | error | timeout | blocked
    token_usage: TokenUsage | None = None
    ttft_ms: float = 0.0  # 首 token 延迟
    estimated_cost_usd: float = 0.0  # 预估费用
    finish_reason: str | None = None  # stop | length | tool_calls | content_filter
    error_message: str | None = None  # status=error/timeout 时的错误详情
    temperature: float | None = None
    max_tokens: int | None = None
    tool_calls_json: str | None = None  # ToolCall 列表序列化 JSON
    guard_hits: list[GuardHitRecord] | None = None
    eval_scores: list[EvalScoreRecord] | None = None
    request_body: dict[str, object] | None = None  # 原始请求 dict（engine 内序列化）
    response_body: dict[str, object] | None = None  # 原始响应 dict
    tool_calls: list[ToolCall] | None = None
    upstream_url: str | None = None
    gateway_version: str | None = None


class TraceSpan(BaseModel):
    """扩展后的 Span 模型 — 所有新增字段都有默认值以保持向后兼容。

    Precondition: trace_id 已存在于 traces 表。
    Postcondition: 通过 store.create_span() 持久化。
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    provider: str
    model: str
    request_hash: str = ""

    # ── P0：内容上下文 ──
    finish_reason: str | None = None
    error_message: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tool_calls_json: str | None = None
    content_id: str | None = None  # 引用 span_contents 表
    request_summary: str | None = None  # 前 200 字符
    response_summary: str | None = None  # 前 500 字符
    request_body_json: str | None = None  # ≤4KB 内联完整请求
    response_body_json: str | None = None  # ≤4KB 内联完整响应

    # ── P1：成本与性能 ──
    is_stream: int = 0  # 0=非流式, 1=流式
    ttft_ms: float = 0.0
    estimated_cost_usd: float = 0.0

    # ── P2：增强分析 ──
    request_path: str = ""

    # ── 升级 P2：结构化 guard/eval ──
    guard_hits_json: str = "[]"  # GuardHitRecord[] JSON
    eval_scores_json: str = "{}"  # EvalScoreRecord[] JSON

    # ── P3：运维元数据 ──
    upstream_url: str | None = None
    gateway_version: str | None = None

    # ── 保持不变的字段 ──
    status: str = "ok"
    token_usage: TokenUsage | None = None
    latency_ms: float = 0.0
    created_at: datetime = Field(default_factory=datetime.now)

    # ── deprecated（保留兼容，新代码使用 _json 后缀字段） ──
    guard_hits: list[str] = Field(default_factory=list)  # type: ignore[assignment]
    eval_scores: dict[str, float] = Field(default_factory=dict)  # type: ignore[assignment]


class RequestContext(BaseModel):
    """Context passed through middleware chain on request."""

    trace_id: str
    span_id: str
    request: NormalizedRequest
    headers: dict[str, str] = Field(default_factory=dict)
    path: str = ""
    provider: str = ""
    guard_results: list[GuardResult] = Field(default_factory=list)
    timeout_deadline: float = 0.0  # set by RequestTimeoutGuard (P3)
    timeout_seconds: float = 0.0


class ResponseContext(BaseModel):
    """Context passed through middleware chain on response."""

    trace_id: str
    span_id: str
    request: NormalizedRequest
    response: NormalizedResponse
    guard_results: list[GuardResult] = Field(default_factory=list)
    eval_results: list[EvalResult] = Field(default_factory=list)


class StreamContext(BaseModel):
    """Context for streaming SSE interception."""

    trace_id: str
    span_id: str
    request: NormalizedRequest
    accumulated_content: str = ""
    guard_results: list[GuardResult] = Field(default_factory=list)
