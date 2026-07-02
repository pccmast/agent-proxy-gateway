"""CircuitBreaker — protects upstream services from cascading failures.

Implements the classic 3-state pattern:
  CLOSED   → normal operation, requests pass through
  OPEN     → failures exceed threshold, requests fail fast (503)
  HALF_OPEN → cooling period elapsed, probes with one request

State transitions:
  CLOSED ──(failures >= threshold)──→ OPEN
  OPEN   ──(timeout elapsed)────────→ HALF_OPEN
  HALF_OPEN ──(probe success)───────→ CLOSED
  HALF_OPEN ──(probe failure)───────→ OPEN
"""

import threading
import time
from enum import StrEnum
from typing import Any

from shared.logging import get_logger

logger = get_logger()


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Protects upstream calls with configurable failure thresholds and recovery."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.failure_threshold: int = failure_threshold
        self.recovery_timeout: float = recovery_timeout
        self.half_open_max_calls: int = half_open_max_calls

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._last_success_time: float = time.monotonic()
        self._half_open_calls: int = 0
        self._lock: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------ public

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CircuitState.OPEN

    def allow_request(self) -> bool:
        """Check if a request should be allowed through.

        Returns False if the breaker is OPEN (no recovery yet) or
        HALF_OPEN and already at max probe calls.
        """
        with self._lock:
            self._transition_state()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                return False

            # HALF_OPEN: allow up to half_open_max_calls
            if self._half_open_calls < self.half_open_max_calls:
                self._half_open_calls += 1
                return True
            return False

    def record_success(self) -> None:
        """Record a successful upstream call."""
        with self._lock:
            self._failure_count = 0
            self._last_success_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                if self._half_open_calls == 0:
                    self._state = CircuitState.CLOSED
                    logger.info("circuit_closed_after_recovery")

    def record_failure(self) -> None:
        """Record a failed upstream call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed → back to OPEN
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                logger.warning("circuit_reopened_after_failed_probe")
                return

            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_opened",
                    failures=self._failure_count,
                    threshold=self.failure_threshold,
                )

    def reset(self) -> None:
        """Force reset to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._half_open_calls = 0

    def get_status(self) -> dict[str, Any]:
        """Return current breaker status for monitoring."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self.failure_threshold,
                "recovery_timeout": self.recovery_timeout,
                "last_failure": self._last_failure_time,
                "last_success": self._last_success_time,
            }

    # --------------------------------------------------------------- internal

    def _transition_state(self) -> None:
        """Check if an OPEN breaker should transition to HALF_OPEN."""
        if self._state != CircuitState.OPEN:
            return

        elapsed = time.monotonic() - self._last_failure_time
        if elapsed >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0
            logger.info("circuit_half_open", recovery_s=round(elapsed, 1))
