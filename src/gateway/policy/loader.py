"""Policy Pydantic models — schema definitions for all YAML configuration sections.

These models validate and type-check the YAML configuration at load time.
Mismatches between config and schema produce clear, early errors.
"""

from pydantic import BaseModel, Field

from shared.constants import DEFAULT_GATEWAY_HOST, DEFAULT_GATEWAY_PORT
from shared.models import GuardAction


class ProviderConfig(BaseModel):
    """Single provider upstream configuration."""

    base_url: str
    api_key_env: str
    default_model: str = ""


class ProxyConfig(BaseModel):
    """Proxy-level configuration."""

    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    upstream_timeout: int = 120
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class TraceConfig(BaseModel):
    """Trace engine configuration."""

    db_path: str = "data/gateway.db"
    max_span_depth: int = 10


class GuardrailRuleConfig(BaseModel):
    """Single guardrail rule definition."""

    id: str
    type: str  # "pii" | "injection" | "content" | "custom"
    action: GuardAction
    confidence_threshold: float = 0.7
    enabled: bool = True
    description: str = ""
    # Injection-specific
    patterns: list[str] | None = None
    # Content-specific
    keywords: list[str] | None = None
    # Custom-specific
    pattern: str | None = None


class GuardrailsConfig(BaseModel):
    """Guardrails section configuration."""

    enabled: bool = True
    rules: list[GuardrailRuleConfig] = Field(default_factory=list)


class BudgetDefaultsConfig(BaseModel):
    """Default budget limits."""

    max_tokens_per_day: int = 1_000_000
    max_tokens_per_hour: int = 100_000
    warning_threshold: float = 0.8


class BudgetConfig(BaseModel):
    """Budget control configuration."""

    defaults: BudgetDefaultsConfig = Field(default_factory=BudgetDefaultsConfig)
    agents: dict[str, BudgetDefaultsConfig] = Field(default_factory=dict)


class RateLimitDefaultsConfig(BaseModel):
    """Default rate limits."""

    rpm: int = 60
    tpm: int = 100_000


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""

    defaults: RateLimitDefaultsConfig = Field(default_factory=RateLimitDefaultsConfig)
    per_model: dict[str, RateLimitDefaultsConfig] = Field(default_factory=dict)


class CircuitBreakerConfig(BaseModel):
    """Circuit breaker configuration."""

    failure_threshold: int = 5
    recovery_timeout: int = 30
    half_open_max_calls: int = 1


class HeuristicEvalConfig(BaseModel):
    """Heuristic evaluation settings."""

    enabled: bool = True
    max_response_length: int = 10000
    repetition_threshold: float = 0.3
    latency_p99_threshold_ms: int = 5000


class LLMJudgeConfig(BaseModel):
    """LLM-as-Judge configuration — experimental, NOT recommended for production.

    Full-quality evaluation should run in a separate offline batch system.
    Enabling this inside the gateway consumes real token costs and competes
    for connection-pool resources — see eval/llm_judge.py for details.
    """

    enabled: bool = False
    model: str = "gpt-4o-mini"
    api_key_env: str = "EVAL_LLM_API_KEY"
    sample_rate: float = 0.1


class EvalConfig(BaseModel):
    """Eval pipeline configuration."""

    heuristic: HeuristicEvalConfig = Field(default_factory=HeuristicEvalConfig)
    llm_judge: LLMJudgeConfig = Field(default_factory=LLMJudgeConfig)


class GatewayPolicy(BaseModel):
    """Full gateway policy — merged from all YAML config files."""

    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)
