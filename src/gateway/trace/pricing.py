"""模型定价表 — 用于 estimated_cost_usd 计算。

每 token 价格定值除以 1e6，避免重复除法运算。
未知模型返回 0.0 不抛异常 — 新模型上线时不应阻塞 trace 写入。
"""

from typing import Final

# ============================================================================
# 定价表常量 — 键为 model 标识符，值为 {"input": $/token, "output": $/token}
# ============================================================================
PRICING_USD_PER_TOKEN: Final[dict[str, dict[str, float]]] = {
    "gpt-4o": {"input": 2.50 / 1e6, "output": 10.00 / 1e6},
    "gpt-4o-mini": {"input": 0.15 / 1e6, "output": 0.60 / 1e6},
    "gpt-4-turbo": {"input": 10.00 / 1e6, "output": 30.00 / 1e6},
    "gpt-3.5-turbo": {"input": 0.50 / 1e6, "output": 1.50 / 1e6},
    "claude-3-opus-20240229": {"input": 15.00 / 1e6, "output": 75.00 / 1e6},
    "claude-3-sonnet-20240229": {"input": 3.00 / 1e6, "output": 15.00 / 1e6},
    "claude-3-haiku-20240307": {"input": 0.25 / 1e6, "output": 1.25 / 1e6},
    # 兜底: 未匹配的模型返回 0.0
}


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """根据模型和 token 数估算单次调用的费用（美元）。

    Precondition:
        prompt_tokens >= 0, completion_tokens >= 0
    Postcondition:
        返回 >= 0 的浮点数，未知模型返回 0.0
    Raises:
        无 — 静默降级为 0.0，不因未知模型阻塞业务流程

    >>> estimate_cost("gpt-4o", 1000, 500)
    0.0075
    >>> estimate_cost("unknown-model", 100, 50)
    0.0
    >>> estimate_cost("gpt-4o", 0, 0)
    0.0
    """
    price = PRICING_USD_PER_TOKEN.get(model)
    if price is None:
        return 0.0
    return prompt_tokens * price["input"] + completion_tokens * price["output"]
