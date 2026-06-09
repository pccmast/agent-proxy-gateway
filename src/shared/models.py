"""Agent Proxy Gateway — shared Pydantic models."""

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class GuardAction(str, Enum):
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


class TraceSpan(BaseModel):
    """A single span in the trace tree."""
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    provider: str
    model: str
    request_hash: str = ""
    status: str = "ok"
    token_usage: TokenUsage | None = None
    latency_ms: float = 0.0
    guard_hits: list[str] = Field(default_factory=list)
    eval_scores: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


class RequestContext(BaseModel):
    """Context passed through middleware chain on request."""
    trace_id: str
    span_id: str
    request: NormalizedRequest
    headers: dict[str, str] = Field(default_factory=dict)
    path: str = ""
    provider: str = ""
    guard_results: list[GuardResult] = Field(default_factory=list)


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