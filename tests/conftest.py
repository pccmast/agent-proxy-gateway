"""Shared test fixtures and configuration."""

import os

# Ensure the src directory is in Python path
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_gateway.db")


@pytest.fixture
def openai_request_body():
    """Sample OpenAI chat completions request."""
    return {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello, world!"},
        ],
        "stream": False,
        "temperature": 0.7,
        "max_tokens": 100,
    }


@pytest.fixture
def openai_stream_request_body():
    """Sample OpenAI streaming request."""
    return {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "Tell me a story."},
        ],
        "stream": True,
    }


@pytest.fixture
def openai_response_body():
    """Sample OpenAI non-streaming response."""
    return {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello! How can I help you today?",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "total_tokens": 28,
        },
    }


@pytest.fixture
def openai_stream_chunks():
    """Sample OpenAI SSE stream chunks (already parsed from 'data:{...}' format)."""
    return [
        '{"id":"chatcmpl-001","choices":[{"delta":{"role":"assistant"},"index":0}]}',
        '{"id":"chatcmpl-001","choices":[{"delta":{"content":"Hello"},"index":0}]}',
        '{"id":"chatcmpl-001","choices":[{"delta":{"content":" World"},"index":0}]}',
        '{"id":"chatcmpl-001","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}',
    ]
