"""Middleware base class + MiddlewareChain — the contract and execution engine for gateway middleware."""

from abc import ABC, abstractmethod
from typing import Callable

from shared.models import RequestContext, ResponseContext, StreamChunk, StreamContext
from shared.logging import get_logger

logger = get_logger()


class BlockException(Exception):
    """Raised when a guardrail decides to block the request/response."""

    def __init__(self, rule_id: str, reason: str, status_code: int = 403):
        self.rule_id = rule_id
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"Blocked by {rule_id}: {reason}")


class RateLimitException(Exception):
    """Raised when rate limiting is exceeded — semantically distinct from BlockException.

    Uses a separate exception class so the proxy layer records
    ``status=\"rate_limited\"`` instead of ``status=\"blocked\"``,
    giving operations teams clear separation between safety incidents
    (403) and quota exhaustion (429).
    """

    def __init__(self, rule_id: str, reason: str, retry_after: float = 1.0):
        self.rule_id = rule_id
        self.reason = reason
        self.status_code = 429
        self.retry_after = retry_after
        super().__init__(f"Rate limited by {rule_id}: {reason}")


class Middleware(ABC):
    """Base class for all gateway middleware.

    Middleware forms a chain that processes requests and responses.
    They are executed in order of priority (lower = earlier).
    """

    priority: int = 100

    @abstractmethod
    async def on_request(self, ctx: RequestContext) -> RequestContext:
        """Process an incoming request before forwarding to the upstream.

        Can modify the request, add metadata, or raise BlockException to reject.
        """
        ...

    @abstractmethod
    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        """Process a response after receiving it from the upstream.

        Can modify the response, record metrics, or trigger evaluations.
        """
        ...

    async def on_stream_chunk(self, chunk: StreamChunk, ctx: StreamContext) -> StreamChunk | None:
        """Process a single SSE chunk during streaming.

        Return the chunk to pass it through, or return None to drop it.
        Default: pass through without modification.
        """
        return chunk


class MiddlewareChain:
    """Ordered chain of middleware that processes requests and responses.

    Responsibilities:
    - Maintain sorted list of middleware by priority
    - Execute on_request chain (request flow: first middleware first)
    - Execute on_response chain (response flow: last middleware first)
    - Handle BlockException from any middleware
    """

    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []

    def add(self, middleware: Middleware) -> "MiddlewareChain":
        """Register a middleware and re-sort by priority."""
        self._middlewares.append(middleware)
        self._middlewares.sort(key=lambda m: m.priority)
        return self

    def add_all(self, middlewares: list[Middleware]) -> "MiddlewareChain":
        """Register multiple middlewares at once."""
        for mw in middlewares:
            self.add(mw)
        return self

    @property
    def middlewares(self) -> list[Middleware]:
        return list(self._middlewares)

    async def run_request(self, ctx: RequestContext) -> RequestContext:
        """Run the request-phase middleware chain.

        Middleware executes in priority order (lowest first).
        Any middleware can raise BlockException to abort.
        """
        for mw in self._middlewares:
            try:
                ctx = await mw.on_request(ctx)
            except (BlockException, RateLimitException):
                raise
            except Exception as e:
                logger.warning(
                    "middleware_error",
                    middleware=type(mw).__name__,
                    phase="request",
                    error=str(e),
                )
        return ctx

    async def run_response(self, ctx: ResponseContext) -> ResponseContext:
        """Run the response-phase middleware chain.

        Middleware executes in priority order (lowest first).
        BlockException is caught and logged but does NOT interrupt the chain
        (response is already sent, so we only record).
        """
        for mw in self._middlewares:
            try:
                ctx = await mw.on_response(ctx)
            except BlockException as e:
                logger.warning(
                    "response_blocked_post_factum",
                    middleware=type(mw).__name__,
                    rule_id=e.rule_id,
                    reason=e.reason,
                )
            except Exception as e:
                logger.warning(
                    "middleware_error",
                    middleware=type(mw).__name__,
                    phase="response",
                    error=str(e),
                )
        return ctx

    async def run_stream_chunk(self, chunk: StreamChunk, ctx: StreamContext) -> StreamChunk | None:
        """Run the streaming chunk through all middleware.

        Each middleware can transform the chunk or return None to drop it.
        If a middleware returns None, the chunk is discarded silently.
        """
        for mw in self._middlewares:
            try:
                result: StreamChunk | None = await mw.on_stream_chunk(chunk, ctx)
                if result is None:
                    return None
                chunk = result
            except BlockException:
                return None
            except Exception as e:
                logger.warning(
                    "middleware_error",
                    middleware=type(mw).__name__,
                    phase="stream_chunk",
                    error=str(e),
                )
            if chunk is None:
                return None
        return chunk
