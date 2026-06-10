"""ScopeMatcher — 规则作用域匹配器 (v2).

判断一条规则是否适用于当前请求（按 model + agent_id 匹配）。
"""

from fnmatch import fnmatch

from .config import RuleScope


class ScopeMatcher:
    """判断规则是否适用于当前请求。

    Precondition: rule_scope.models/agents 已从配置中加载。
    Postcondition: 返回 bool，调用方据此决定是否执行该规则。
    """

    @staticmethod
    def matches(
        rule_scope: RuleScope,
        model: str,
        agent_id: str = "default",
    ) -> bool:
        """检查 model 和 agent_id 是否在 rule_scope 内。

        ["*"] 匹配所有。支持 fnmatch glob 模式（如 "gpt-4o*"）。

        Returns:
            True 当 model 和 agent_id 都在 scope 内。
        """
        # 检查 model
        if not ScopeMatcher._matches_any(rule_scope.models, model):
            return False
        # 检查 agent
        if not ScopeMatcher._matches_any(rule_scope.agents, agent_id):
            return False
        return True

    @staticmethod
    def _matches_any(patterns: list[str], value: str) -> bool:
        """value 是否匹配 patterns 中的任意一个。

        - patterns 为 ["*"] 表示通配
        - patterns 中的每个元素可以是 glob 模式或精确字符串
        """
        if not patterns or patterns == ["*"]:
            return True
        return any(fnmatch(value, p) for p in patterns)
