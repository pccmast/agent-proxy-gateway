"""Base class for guardrail rules — v2 升级版.

支持 phase 声明、scope 感知、插件式自动发现。
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from shared.models import GuardAction, GuardResult

if TYPE_CHECKING:
    from ..config import RuleScope, SessionState


class BaseGuardRule(ABC):
    """所有安全规则的抽象基类 — v2 升级版。

    Precondition: 子类必须声明 rule_type 类属性。
    Postcondition: engine 通过 discover_rules() 发现所有实现。
    """

    rule_type: str = ""  # 如 "injection"、"pii"
    rule_id: str = ""
    action: GuardAction = GuardAction.LOG
    confidence_threshold: float = 0.7
    enabled: bool = True
    severity: str = "medium"  # [新增]

    def __init__(
        self,
        rule_id: str | None = None,
        action: GuardAction | str | None = None,
        severity: str = "medium",
        confidence_threshold: float = 0.7,
        enabled: bool = True,
        scope: "RuleScope | None" = None,  # [新增]
        config: dict[str, object] | None = None,  # [新增] 类型特定配置
    ) -> None:
        # 仅在显式传入时覆盖子类默认值
        if rule_id is not None:
            self.rule_id = rule_id
        if action is not None:
            if isinstance(action, str):
                self.action = GuardAction(action)
            else:
                self.action = action
        self.severity = severity
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled
        self._scope = scope
        self._config = config or {}

    def is_enabled(self) -> bool:
        return self.enabled

    def get_action(self) -> GuardAction:
        return self.action

    @property
    def scope(self) -> "RuleScope | None":
        return self._scope

    @abstractmethod
    async def check_input(
        self,
        text: str,
        session: "SessionState | None" = None,
    ) -> GuardResult:
        """检查输入文本。

        Precondition: text 为完整的用户输入。
        Postcondition: 返回 GuardResult（matches 为空表示无命中）。
        """
        ...

    @abstractmethod
    async def check_output(
        self,
        text: str,
        session: "SessionState | None" = None,
    ) -> GuardResult:
        """检查输出文本。

        Precondition: text 为完整的模型输出。
        Postcondition: 返回 GuardResult。
        """
        ...

    # ------------------------------------------------------------------
    # 插件发现
    # ------------------------------------------------------------------

    @classmethod
    def discover_rules(cls) -> dict[str, type["BaseGuardRule"]]:
        """扫描 rules/ 目录，自动发现所有 BaseGuardRule 子类。

        Postcondition: 返回 {rule_type: RuleClass} 映射。
        """
        import importlib
        from pathlib import Path

        registry: dict[str, type[BaseGuardRule]] = {}
        rules_dir = Path(__file__).parent

        for file in rules_dir.glob("*.py"):
            if file.name.startswith("_") or file.name == "base.py":
                continue
            module_name = file.stem
            try:
                module = importlib.import_module(f".{module_name}", package=__package__)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseGuardRule)
                        and attr is not BaseGuardRule
                        and attr.rule_type
                    ):
                        registry[attr.rule_type] = attr
            except ImportError:
                continue

        return registry
