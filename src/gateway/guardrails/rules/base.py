"""Base class for guardrail rules."""

from abc import ABC, abstractmethod

from shared.models import GuardResult, GuardAction


class BaseGuardRule(ABC):
    """A single guardrail check that inspects text content."""

    rule_id: str = ""
    action: GuardAction = GuardAction.LOG
    confidence_threshold: float = 0.7
    enabled: bool = True

    def is_enabled(self) -> bool:
        return self.enabled

    @abstractmethod
    async def check_input(self, text: str) -> GuardResult:
        """Inspect input (user prompt / messages) for violations."""
        ...

    @abstractmethod
    async def check_output(self, text: str) -> GuardResult:
        """Inspect output (model response) for violations."""
        ...
