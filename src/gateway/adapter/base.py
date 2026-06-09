"""Protocol Adapter — base class and registry for LLM provider adapters."""

from abc import ABC, abstractmethod
from typing import Any
from fastapi import Request
from shared.models import NormalizedRequest, NormalizedResponse, StreamChunk


class ProtocolAdapter(ABC):
    """Abstract base for LLM/Tool protocol adapters.

    Each adapter handles one provider (OpenAI, Anthropic, etc.)
    and converts between provider-specific format and our Normalized schema.
    """

    provider: str = ""

    @abstractmethod
    def can_handle(self, request: Request) -> bool:
        """Check if this adapter can handle the incoming request.

        Typically checks the request path and headers.
        """
        ...

    @abstractmethod
    async def normalize_request(self, raw_body: dict[str, Any], headers: dict[str, str], path: str) -> NormalizedRequest:
        """Convert provider-specific request to NormalizedRequest."""
        ...

    @abstractmethod
    def normalize_response(self, raw_body: dict[str, Any]) -> NormalizedResponse:
        """Convert provider-specific response to NormalizedResponse."""
        ...

    @abstractmethod
    def extract_stream_chunk(self, sse_data: str) -> StreamChunk | None:
        """Parse a single SSE data line into a StreamChunk.

        SSE lines are in format: data: {json}\n\n
        Returns None for non-data lines (comments, empty lines).
        """
        ...

    @abstractmethod
    def get_upstream_url(self, path: str, base_url: str) -> str:
        """Build the full upstream URL for forwarding."""
        ...

    @abstractmethod
    def get_upstream_headers(self, original_headers: dict[str, str], api_key: str) -> dict[str, str]:
        """Build headers for the upstream request.

        Replace auth headers with real API key, preserve others.
        """
        ...


class AdapterRegistry:
    """Registry of available protocol adapters.

    Iterates adapters in priority order and selects the first one
    that can_handle() the request.
    """

    def __init__(self):
        self._adapters: dict[str, ProtocolAdapter] = {}

    def register(self, adapter: ProtocolAdapter) -> None:
        self._adapters[adapter.provider] = adapter

    def resolve(self, request: Request) -> ProtocolAdapter | None:
        """Find the adapter that can handle this request."""
        for adapter in self._adapters.values():
            if adapter.can_handle(request):
                return adapter
        return None

    def get(self, provider: str) -> ProtocolAdapter | None:
        return self._adapters.get(provider)

    def list_providers(self) -> list[str]:
        return list(self._adapters.keys())