"""Unit tests for OpenAI protocol adapter."""

import pytest
from unittest.mock import MagicMock

from gateway.adapter.openai import OpenAIAdapter


class TestOpenAIAdapter:
    """Tests for OpenAIAdapter protocol conversion."""

    @pytest.fixture
    def adapter(self):
        return OpenAIAdapter()

    def test_can_handle_chat_completions_path(self, adapter):
        """Should recognize /v1/chat/completions."""
        mock_request = MagicMock()
        mock_request.url.path = "/v1/chat/completions"
        mock_request.headers = {}
        assert adapter.can_handle(mock_request) is True

    def test_can_handle_completions_path(self, adapter):
        """Should recognize /v1/completions."""
        mock_request = MagicMock()
        mock_request.url.path = "/v1/completions"
        mock_request.headers = {}
        assert adapter.can_handle(mock_request) is True

    def test_can_handle_sk_key_header(self, adapter):
        """Should recognize Authorization: Bearer sk-... header."""
        mock_request = MagicMock()
        mock_request.url.path = "/some/custom/path"
        mock_request.headers = {"Authorization": "Bearer sk-test123"}
        assert adapter.can_handle(mock_request) is True

    def test_cannot_handle_unknown_path(self, adapter):
        """Should NOT recognize unknown paths without API key header."""
        mock_request = MagicMock()
        mock_request.url.path = "/api/v1/custom"
        mock_request.headers = {}
        assert adapter.can_handle(mock_request) is False

    @pytest.mark.asyncio
    async def test_normalize_request_basic(self, adapter, openai_request_body):
        """Should normalize a basic chat completions request."""
        result = await adapter.normalize_request(
            openai_request_body,
            headers={"Content-Type": "application/json"},
            path="/v1/chat/completions",
        )

        assert result.provider == "openai"
        assert result.model == "gpt-4o"
        assert len(result.messages) == 2
        assert result.messages[0].role == "system"
        assert result.messages[0].content == "You are a helpful assistant."
        assert result.messages[1].role == "user"
        assert result.stream is False
        assert result.temperature == 0.7
        assert result.max_tokens == 100

    @pytest.mark.asyncio
    async def test_normalize_request_streaming(self, adapter, openai_stream_request_body):
        """Should set stream=True for streaming requests."""
        result = await adapter.normalize_request(
            openai_stream_request_body,
            headers={},
            path="/v1/chat/completions",
        )
        assert result.stream is True

    def test_normalize_response(self, adapter, openai_response_body):
        """Should normalize an OpenAI chat completion response."""
        result = adapter.normalize_response(openai_response_body)

        assert result.provider == "openai"
        assert result.content == "Hello! How can I help you today?"
        assert result.finish_reason == "stop"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 20
        assert result.usage.completion_tokens == 8
        assert result.usage.total_tokens == 28

    def test_extract_stream_chunk_content(self, adapter):
        """Should extract delta content from SSE chunk."""
        chunk = adapter.extract_stream_chunk(
            '{"choices":[{"delta":{"content":"Hello"},"index":0}]}'
        )
        assert chunk is not None
        assert chunk.delta_content == "Hello"
        assert chunk.is_done is False

    def test_extract_stream_chunk_done(self, adapter):
        """Should detect [DONE] signal."""
        chunk = adapter.extract_stream_chunk("[DONE]")
        assert chunk is not None
        assert chunk.is_done is True

    def test_extract_stream_chunk_usage(self, adapter):
        """Should extract usage from final chunk."""
        chunk = adapter.extract_stream_chunk(
            '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
        )
        assert chunk is not None
        assert chunk.usage is not None
        assert chunk.usage.total_tokens == 15
        assert chunk.finish_reason == "stop"

    def test_extract_stream_chunk_empty(self, adapter):
        """Should return None for empty/whitespace-only data."""
        assert adapter.extract_stream_chunk("") is None
        assert adapter.extract_stream_chunk("   ") is None

    def test_get_upstream_url(self, adapter):
        """Should build correct upstream URL."""
        url = adapter.get_upstream_url("/v1/chat/completions", "https://api.openai.com")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_get_upstream_headers(self, adapter):
        """Should replace auth headers with real API key."""
        headers = adapter.get_upstream_headers(
            {"Content-Type": "application/json", "Authorization": "Bearer fake-key"},
            api_key="sk-real-key",
        )
        assert headers["Authorization"] == "Bearer sk-real-key"
        assert headers["Content-Type"] == "application/json"
        assert "Host" in headers
