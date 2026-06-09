"""Content safety rule — detects harmful, violent, or illegal content via keyword matching."""

import re
from shared.models import GuardResult, GuardAction
from .base import BaseGuardRule

# Default keywords — categorized for clarity
_DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "violence": [
        "kill", "murder", "massacre", "slaughter", "torture", "maim",
        "assassinate", "execute", "bomb", "shoot", "stab",
    ],
    "self_harm": [
        "suicide", "self-harm", "self harm", "cutting myself",
        "kill myself", "end my life", "hurt myself",
    ],
    "illegal": [
        "counterfeit", "money laundering", "traffic", "smuggle",
        "ransomware", "phishing kit", "exploit kit",
    ],
    "hate": [
        "hate speech", "racial slur", "discrimination", "supremacist",
    ],
}

# Compile all keywords into a single regex for O(n) scanning
_ALL_KEYWORDS: list[str] = []
for _category, _words in _DEFAULT_KEYWORDS.items():
    _ALL_KEYWORDS.extend(_words)


class ContentSafetyRule(BaseGuardRule):
    """Detects unsafe content using configurable keyword blacklists.

    Categories: violence, self-harm, illegal activities, hate speech.
    Each match is scored with a category-specific confidence.
    """

    rule_id: str = "content-safety"
    action: GuardAction = GuardAction.BLOCK

    def __init__(
        self,
        keywords: list[str] | None = None,
        confidence_threshold: float = 0.6,
        enabled: bool = True,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled
        kw_list = keywords if keywords else _ALL_KEYWORDS
        # Build a single regex for fast scanning
        escaped = [re.escape(kw) for kw in kw_list]
        self._pattern = re.compile(
            r"\b(?:" + "|".join(escaped) + r")\b",
            re.IGNORECASE,
        )

    async def check_input(self, text: str) -> GuardResult:
        return self._check(text, phase="input")

    async def check_output(self, text: str) -> GuardResult:
        return self._check(text, phase="output")

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matches_all = self._pattern.findall(text)
        matches = list(dict.fromkeys(matches_all))  # deduplicate preserving order

        # Confidence scales with match count and distinct categories hit
        categories_hit = set()
        for m in matches:
            m_lower = m.lower()
            for category, words in _DEFAULT_KEYWORDS.items():
                if m_lower in words:
                    categories_hit.add(category)

        base = min(len(matches) * 0.15, 0.4)
        cat_bonus = min(len(categories_hit) * 0.15, 0.3)
        confidence = base + cat_bonus

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action,
            matches=matches,
            confidence=confidence,
            details=(
                f"[{phase}] {len(matches)} match(es) in {len(categories_hit)} categories: "
                f"{', '.join(sorted(categories_hit))}"
            ),
        )
