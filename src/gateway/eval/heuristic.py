"""Heuristic evaluators — zero-cost, deterministic quality checks.

Each evaluator runs synchronously on every response and returns an EvalResult.
They are designed to be fast (O(n) at worst) and non-blocking.
"""

import re
from collections import Counter

from shared.models import EvalResult, NormalizedResponse


class ResponseLengthEval:
    """Evaluate response length — flags abnormally short or long outputs."""

    name: str = "response_length"

    def __init__(self, max_response_length: int = 10_000) -> None:
        self.max_response_length = max_response_length

    def evaluate(self, response: NormalizedResponse) -> EvalResult:
        content = response.content or ""
        length = len(content)

        if length == 0:
            return EvalResult(name=self.name, score=0.0, details="Empty response")

        if length > self.max_response_length:
            return EvalResult(
                name=self.name,
                score=0.3,
                details=f"Response too long: {length} chars (max {self.max_response_length})",
            )

        if length < 10:
            return EvalResult(
                name=self.name,
                score=0.5,
                details=f"Response unusually short: {length} chars",
            )

        return EvalResult(name=self.name, score=1.0, details=f"OK ({length} chars)")


class RepetitionEval:
    """Evaluate response repetition — detects regurgitated content.

    Uses n-gram (word-level bigram) overlap to detect repetitive patterns.
    """

    name: str = "repetition"

    def __init__(self, repetition_threshold: float = 0.3) -> None:
        self.threshold = repetition_threshold

    def evaluate(self, response: NormalizedResponse) -> EvalResult:
        content = response.content or ""
        if not content.strip():
            return EvalResult(name=self.name, score=1.0, details="Empty response — no repetition")

        # Split into word-like tokens
        words = [w.lower() for w in re.findall(r"\w+", content)]
        if len(words) < 4:
            return EvalResult(name=self.name, score=1.0, details="Too short to assess")

        # Count bigrams
        bigrams = [f"{words[i]}:{words[i+1]}" for i in range(len(words) - 1)]
        if not bigrams:
            return EvalResult(name=self.name, score=1.0, details="No bigrams")

        bigram_counts = Counter(bigrams)
        repeats = sum(c - 1 for c in bigram_counts.values() if c > 1)
        ratio = repeats / len(bigrams) if bigrams else 0.0

        score = max(0.0, 1.0 - ratio / self.threshold)
        return EvalResult(
            name=self.name,
            score=round(score, 3),
            details=(
                f"Repetition ratio: {ratio:.3f} "
                f"(threshold: {self.threshold})"
            ),
        )


class LatencyEval:
    """Evaluate response latency — flags abnormally high latency."""

    name: str = "latency"

    def __init__(self, p99_threshold_ms: float = 5_000.0) -> None:
        self.p99_threshold_ms = p99_threshold_ms

    def evaluate(self, response: NormalizedResponse, latency_ms: float = 0.0) -> EvalResult:
        if latency_ms <= 0:
            return EvalResult(name=self.name, score=1.0, details="No latency data")

        if latency_ms > self.p99_threshold_ms:
            score = max(0.0, self.p99_threshold_ms / latency_ms)
            return EvalResult(
                name=self.name,
                score=round(score, 3),
                details=f"High latency: {latency_ms:.0f}ms (P99 threshold: {self.p99_threshold_ms:.0f}ms)",
            )

        return EvalResult(name=self.name, score=1.0, details=f"OK ({latency_ms:.0f}ms)")


class ToolCallEval:
    """Evaluate tool call quality — checks for completeness and dead loops.

    Flags:
    - Tool calls with empty arguments
    - Repeated identical tool calls (potential dead loop)
    """

    name: str = "tool_call"

    def evaluate(self, response: NormalizedResponse) -> EvalResult:
        tool_calls = response.tool_calls or []
        if not tool_calls:
            return EvalResult(name=self.name, score=1.0, details="No tool calls — N/A")

        issues: list[str] = []

        # Check for empty arguments
        empty_args = [tc.name for tc in tool_calls if not tc.arguments]
        if empty_args:
            issues.append(f"Empty arguments in: {', '.join(empty_args)}")

        # Check for duplicate calls (same name + same args)
        seen: set[tuple[str, str]] = set()
        for tc in tool_calls:
            sig = (tc.name, str(sorted(tc.arguments.items())))
            if sig in seen:
                issues.append(f"Duplicate call: {tc.name}")
            seen.add(sig)

        if not issues:
            return EvalResult(
                name=self.name,
                score=1.0,
                details=f"All {len(tool_calls)} tool call(s) OK",
            )

        return EvalResult(
            name=self.name,
            score=max(0.0, 1.0 - len(issues) * 0.3),
            details="; ".join(issues),
        )
