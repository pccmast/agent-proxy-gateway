"""CredentialLeakRule — detect leaked secrets in prompts.

Catches common developer mistakes: API keys, JWT tokens, access-key pairs,
and connection strings accidentally pasted into system prompts or user
messages. These leaks end up in LLM Backend logs — a compliance incident.

Uses regex fast-path matching (no ML dependency).
Recommended action: REDACT — replace with placeholder but don't block
the request, since the leak is typically unintentional.
"""

import re
from typing import TYPE_CHECKING

from shared.logging import get_logger
from shared.models import GuardAction, GuardResult

from .base import BaseGuardRule

if TYPE_CHECKING:
    from ..config import SessionState

logger = get_logger()

# Patterns ordered from most-specific to least-specific to avoid
# substring shadowing (e.g. 'Bearer xxx' before generic token match).
_CREDENTIAL_PATTERNS: list[tuple[str, str, float]] = [
    # OpenAI / Skater API keys
    (r"sk-[a-zA-Z0-9]{32,}", "openai_api_key", 0.95),
    # Anthropic API key (sk-ant-xxx)
    (r"sk-ant-[a-zA-Z0-9]{32,}", "anthropic_api_key", 0.95),
    # Google / generic cloud API keys
    (r"AIza[0-9A-Za-z\-_]{35}", "google_api_key", 0.90),
    # Bearer JWT tokens (>= 40 base64 chars with signature segments)
    (r"Bearer\s+eyJ[a-zA-Z0-9\-_]{20,}\.[a-zA-Z0-9\-_]{20,}\.[a-zA-Z0-9\-_]{10,}", "jwt_bearer", 0.92),
    # AWS Access Key ID (AKIA... or ASIA... 20 chars)
    (r"\b(AKIA|ASIA)[A-Z0-9]{16}\b", "aws_access_key", 0.88),
    # AWS Secret Access Key (40 base64 chars)
    (r"\b(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])", "aws_secret_key", 0.70),
    # Generic key=value secrets (long hex/base64 values)
    (
        r"(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\'\"][^\'\"]{16,}[\'\"]",
        "key_value_secret",
        0.80,
    ),
    # Connection strings (SQL / MongoDB / Redis)
    (r"\b(?:mongodb|mysql|postgresql|redis)://[^@\s]+:[^@\s]+@[^\s]+", "connection_string", 0.85),
    # Private key headers (SSH / PEM / PGP)
    (r"-----BEGIN\s+(?:RSA|DSA|EC|OPENSSH|PGP)\s+PRIVATE\s+KEY-----", "private_key", 0.95),
]


class CredentialLeakRule(BaseGuardRule):
    """Detect leaked credentials in user and system prompts.

    Checks both input (user/system messages) and output (model responses
    that might regurgitate secrets from training data).

    By default uses REDACT action — replaces matches with a placeholder
    rather than blocking the request, since credential leaks are usually
    unintentional developer mistakes, not attacks.
    """

    rule_type: str = "credential_leak"
    rule_id: str = "credential-leak"
    action: GuardAction = GuardAction.REDACT

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.confidence_threshold = confidence_threshold
        self.enabled = enabled

    # ------------------------------------------------------------------ rule API

    async def check_input(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="input")

    async def check_output(self, text: str, session: "SessionState | None" = None) -> GuardResult:
        return self._check(text, phase="output")

    # ---------------------------------------------------------------- internal

    def _check(self, text: str, phase: str) -> GuardResult:
        if not text:
            return GuardResult(rule_id=self.rule_id, action=self.action)

        matches: list[str] = []
        max_confidence = 0.0
        match_details: list[str] = []

        for pattern, label, base_confidence in _CREDENTIAL_PATTERNS:
            for m in re.finditer(pattern, text):
                match_text = m.group()
                matches.append(match_text)
                max_confidence = max(max_confidence, base_confidence)
                match_details.append(f"{label}:{match_text[:24]}...")

        # Honour confidence threshold: weak matches are downgraded to LOG
        effective_action = self.action
        if matches and max_confidence < self.confidence_threshold:
            effective_action = GuardAction.LOG

        return GuardResult(
            rule_id=self.rule_id,
            action=effective_action,
            matches=matches,
            confidence=max_confidence,
            details=f"[{phase}] {len(matches)} credential(s) detected | "
            + ("; ".join(match_details[:5]) if match_details else "none"),
        )
