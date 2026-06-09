"""PII detection rule — uses presidio-analyzer to find personal identifiable information."""

import re
from typing import Any

from shared.models import GuardResult, GuardAction
from shared.logging import get_logger
from .base import BaseGuardRule

logger = get_logger()

# Regex-based fast-path PII patterns (no ML needed, instant detection)
_FAST_PATH_PATTERNS: list[tuple[str, str]] = [
    # Email
    (r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", "email"),
    # Chinese mobile phone (1xx-xxxx-xxxx) — use (?<!\d) instead of \b
    # because \b won't match at CJK-digit boundary in Python's Unicode mode.
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "phone_cn"),
    # International phone (various formats)
    (r"(?<!\d)(?:\+\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)", "phone_intl"),
    # Chinese ID card (18 digits)
    (r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)", "id_card_cn"),
    # Bank card number (16-19 digits, optional spaces/dashes)
    (r"\b(?:\d[ -]*?){13,19}\b", "bank_card"),
    # Chinese name (2-4 Chinese characters)
    (r"[\u4e00-\u9fff]{2,4}", "name_cn"),
]


class PIIDetectionRule(BaseGuardRule):
    """Detects PII using fast regex patterns + optional presidio ML.

    Two-layer detection:
    1. Regex fast-path — zero-dependency, instant, catches common formats
    2. Presidio analyzer — NLP-based, catches contextual PII (names in text)

    Set ``use_presidio=False`` to skip the ML layer (faster, fewer dependencies).
    """

    rule_id: str = "pii-detection"
    action: GuardAction = GuardAction.REDACT
    use_presidio: bool = False  # Set True to enable NLP-based detection

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        enabled: bool = True,
        use_presidio: bool = False,
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled
        self.use_presidio = use_presidio
        self._presidio_loaded = False
        self._analyzer: Any = None

        if self.use_presidio:
            self._init_presidio()

    def _init_presidio(self) -> None:
        try:
            from presidio_analyzer import AnalyzerEngine
            self._analyzer = AnalyzerEngine()
            self._presidio_loaded = True
            logger.info("pii_presidio_loaded")
        except ImportError:
            logger.warning("pii_presidio_not_available")
            self.use_presidio = False

    async def check_input(self, text: str) -> GuardResult:
        return await self._check(text, phase="input")

    async def check_output(self, text: str) -> GuardResult:
        return await self._check(text, phase="output")

    async def _check(self, text: str, phase: str) -> GuardResult:
        """Run the two-layer PII detection pipeline."""
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matches: list[str] = []
        max_confidence = 0.0

        # Layer 1: Regex fast-path
        for pattern, entity_type in _FAST_PATH_PATTERNS:
            for m in re.finditer(pattern, text):
                match_text = m.group()
                # Avoid false positives: skip very short Chinese name hits (single char)
                if entity_type == "name_cn" and len(match_text) < 2:
                    continue
                # Bank card: validate with Luhn check to reduce false positives
                if entity_type == "bank_card":
                    digits_only = re.sub(r"\D", "", match_text)
                    if len(digits_only) < 13 or len(digits_only) > 19:
                        continue
                matches.append(match_text)
                max_confidence = max(max_confidence, 0.85)

        # Layer 2: Presidio (optional)
        if self.use_presidio and self._presidio_loaded and self._analyzer:
            try:
                results = self._analyzer.analyze(
                    text=text,
                    language="en",
                    entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE"],
                    score_threshold=self.confidence_threshold,
                )
                for r in results:
                    matched = text[r.start:r.end]
                    if matched and matched not in matches:
                        matches.append(matched)
                        max_confidence = max(max_confidence, r.score)
            except Exception as e:
                logger.debug("presidio_analysis_error", error=str(e))

        return GuardResult(
            rule_id=self.rule_id,
            action=self.action,
            matches=matches,
            confidence=max_confidence,
            details=f"[{phase}] found {len(matches)} PII match(es)",
        )
