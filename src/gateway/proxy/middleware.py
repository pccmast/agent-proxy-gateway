"""Middleware base class — the contract every gateway middleware must implement."""

from abc import ABC, abstractmethod
from shared.models import RequestContext, ResponseContext, StreamChunk, StreamContext


class BlockException(Exception):
    """Raised when a guardrail decides to block the request/response."""

    def __init__(self, rule_id: str, reason: str, status_code: int = 403):
        self.rule_id = rule_id
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"Blocked by {rule_id}: {reason}")


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