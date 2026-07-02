"""AI Safety Platform — Pydantic 配置模型 (v2).

定义 RuleScope, BaseRuleConfig, 各规则类型的 *Config,
SessionState, AuditEvent, SafetyPlatformConfig 等模型。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ============================================================================
# 枚举
# ============================================================================


class RulePhase(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ============================================================================
# RuleScope
# ============================================================================


class RuleScope(BaseModel):
    """规则作用范围。

    Precondition: models 和 agents 为合法值。
    Postcondition: ScopeMatcher.matches() 可据此判断规则是否适用于当前请求。
    """

    models: list[str] = Field(default_factory=lambda: ["*"])
    agents: list[str] = Field(default_factory=lambda: ["*"])


# ============================================================================
# BaseRuleConfig
# ============================================================================


class BaseRuleConfig(BaseModel):
    """所有规则配置的通用字段。

    Precondition: type 值在插件注册表中存在对应的规则实现。
    Postcondition: engine 根据此配置实例化对应规则。
    """

    id: str
    type: str
    phase: str = "input"
    action: str = "block"
    severity: str = "medium"
    confidence_threshold: float = 0.7
    enabled: bool = True
    scope: RuleScope = Field(default_factory=RuleScope)


# ============================================================================
# 具体规则的 config 子对象
# ============================================================================


class InjectionConfig(BaseModel):
    """注入检测配置"""

    pattern_matching: bool = True
    patterns: list[str] = Field(default_factory=list)
    semantic_classifier: bool = False
    semantic_threshold: float = 0.7
    indirect_injection: bool = False
    check_context_documents: bool = False


class PIIConfig(BaseModel):
    """PII 检测配置"""

    structured_pii: bool = True
    categories: list[str] = Field(default_factory=lambda: ["email", "phone", "id_card", "bank_card"])
    secrets_detection: bool = False
    secret_patterns: list[dict[str, str]] = Field(default_factory=list)
    custom_terms_file: str = ""


class SystemPromptConfig(BaseModel):
    """系统提示相关规则的配置"""

    patterns: list[str] = Field(default_factory=list)
    similarity_check: bool = False
    system_prompt_hash: str = ""
    similarity_threshold: float = 0.85
    # system_prompt_leakage 用 n-gram 检测
    ngram_n: int = 5
    ngram_threshold: float = 0.3
    key_phrases: list[str] = Field(default_factory=list)
    match_threshold: int = 2


class TopicConfig(BaseModel):
    """主题限制配置"""

    allowed_topics: list[str] = Field(default_factory=list)
    fallback_action: str = "log"


class AgencyConfig(BaseModel):
    """过度代理配置"""

    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    parameter_deny_patterns: list[dict[str, object]] = Field(default_factory=list)


class FormatValidationConfig(BaseModel):
    """输出格式校验配置"""

    expected_format: str = "text"
    json_schema: dict[str, object] | None = None
    on_mismatch: str = "log"


class HallucinationConfig(BaseModel):
    """幻觉指标配置"""

    url_validation: bool = False
    verify_dns: bool = False
    url_timeout_ms: int = 2000
    citation_patterns: list[str] = Field(default_factory=list)
    numeric_consistency: bool = False


class JailbreakConfig(BaseModel):
    """多轮越狱检测配置"""

    session_timeout_minutes: int = 30
    max_history_turns: int = 20
    escalation_signals: list[dict[str, object]] = Field(default_factory=list)
    escalation_threshold: float = 0.8
    on_trigger_reset_session: bool = True


class ToolLoopConfig(BaseModel):
    """工具调用循环检测配置"""

    max_consecutive_same_tool: int = 3
    max_cycle_length: int = 4
    max_total_tool_calls: int = 50


class AnomalyConfig(BaseModel):
    """异常检测配置"""

    baselines: dict[str, object] = Field(default_factory=dict)
    learning_period_hours: int = 168
    alert_at_sigma: float = 3.0


# ============================================================================
# SafetyPlatformConfig（聚合配置）
# ============================================================================


class SafetyPlatformConfig(BaseModel):
    """AI Safety Platform 完整配置。

    Precondition: YAML 文件格式正确。
    Postcondition: engine 据此加载所有规则。
    """

    version: str = "2.0"
    enabled: bool = True
    input_rules: list[dict[str, object]] = Field(default_factory=list)
    output_rules: list[dict[str, object]] = Field(default_factory=list)
    behavioral_rules: list[dict[str, object]] = Field(default_factory=list)
    audit: dict[str, object] = Field(default_factory=dict)


# ============================================================================
# SessionState（供 SessionStore 使用）
# ============================================================================


@dataclass
class SessionState:
    """单次会话的安全状态。

    Postcondition: SessionStore 根据此对象追踪跨请求攻击模式。
    """

    session_id: str
    escalation_score: float = 0.0
    history: list[dict[str, str]] = field(default_factory=list)
    tool_call_history: list[dict[str, object]] = field(default_factory=list)
    consecutive_same_tool: int = 0
    total_tool_calls: int = 0
    last_activity: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)


# ============================================================================
# AuditEvent（供 AuditLogger 使用）
# ============================================================================


class AuditEvent(BaseModel):
    """一次安全事件的审计记录。

    Postcondition: AuditLogger 将其持久化到审计存储。
    """

    event_id: str
    event_type: str = ""  # guard_hit | block | redact | config_change
    rule_id: str = ""
    session_id: str | None = None
    trace_id: str | None = None
    severity: str = "medium"
    details: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
