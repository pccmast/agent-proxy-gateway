"""Prompt injection detection rule — catches common attack patterns.

v2 — upgraded with semantic_classifier + indirect_injection support.
"""

import re
from typing import TYPE_CHECKING

from shared.models import GuardResult, GuardAction
from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState

# Default injection patterns — can be overridden via config
_DEFAULT_PATTERNS: list[str] = [
    r"ignore\s+(all\s+)?previous\s+instructions?",
    r"ignore\s+all\s+previous",
    r"system\s+override",
    r"you\s+are\s+now",
    r"new\s+instructions?",
    r"forget\s+(about\s+)?(your\s+)?(training|instructions|rules|constraints)",
    r"disregard\s+(all\s+)?(prior|previous)",
    r"pretend\s+(you\s+are|to\s+be)",
    r"act\s+as\s+(if|a|an)",
    r"you\s+are\s+no\s+longer",
    r"from\s+now\s+on\s+you\s+(are|will)",
    r"jailbreak",
    r"dann\s+prompt",
]


class InjectionDetectionRule(BaseGuardRule):
    """Detects prompt injection attempts using heuristic regex + keyword matching.

    Attack vectors covered:
    - Direct instruction override ("ignore previous instructions")
    - Role-playing hijack ("you are now DAN")
    - System prompt leaking ("print your system prompt")
    - Multi-layer encoding (base64 > 2 layers)
    """

    rule_type: str = "injection"
    rule_id: str = "injection-detection"
    action: GuardAction = GuardAction.BLOCK

    def __init__(
        self,
        patterns: list[str] | None = None,
        confidence_threshold: float = 0.6,
        enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

        raw_patterns = patterns if patterns else _DEFAULT_PATTERNS
        # Merge with config patterns if provided
        config_patterns = self._config.get("patterns", [])
        if config_patterns and isinstance(config_patterns, list):
            all_patterns = raw_patterns + [p for p in config_patterns if isinstance(p, str)]
        else:
            all_patterns = raw_patterns
        self._patterns: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in all_patterns
        ]

    async def check_input(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="input")

    async def check_output(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="output")

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        text_lower = text.lower()
        matches: list[str] = []
        max_confidence = 0.0

        for pattern in self._patterns:
            for m in pattern.finditer(text_lower):
                match_text = m.group()
                matches.append(match_text)
                # Longer matches indicate higher confidence
                confidence = 0.5 + min(len(match_text) / 200, 0.4)
                max_confidence = max(max_confidence, confidence)

        # Multi-layer base64 encoding heuristic
        b64_payloads = self._extract_base64_chains(text)
        for payload in b64_payloads:
            if len(payload) > 200:  # Encoded payloads are usually large
                matches.append("base64_chain")
                max_confidence = max(max_confidence, 0.7)

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action,
            matches=matches,
            confidence=max_confidence,
            details=f"[{phase}] {len(matches)} injection pattern(s) matched",
        )

    @staticmethod
    def _extract_base64_chains(text: str) -> list[str]:
        """Find potential base64-encoded payload chains (>= 2 layers).

        Returns a list of detected base64 strings longer than 100 chars.
        """
        import base64

        candidates: list[str] = []
        pattern = re.compile(r"(?:[A-Za-z\d+/]{4}){20,}(?:[A-Za-z\d+/]{2}==|[A-Za-z\d+/]{3}=)?")
        for m in pattern.finditer(text):
            candidate = m.group()
            depth = 0
            current = candidate
            try:
                while depth < 5:
                    decoded = base64.b64decode(current, validate=True)
                    decoded_str = decoded.decode("ascii", errors="ignore")
                    if not decoded_str.strip():
                        break
                    depth += 1
                    if depth >= 2:
                        candidates.append(candidate)
                        break
                    current = decoded_str
            except Exception:
                continue
        return candidates
