"""OpenAI protocol adapter — maps OpenAI chat completions to NormalizedRequest/Response."""

# pyright: reportImplicitOverride=false, reportExplicitAny=false, reportUnnecessaryCast=false, reportAny=false

import json
from typing import Any, cast

from fastapi import Request

from shared.models import (
    Message,
    NormalizedRequest,
    NormalizedResponse,
    StreamChunk,
    TokenUsage,
    ToolCall,
    ToolDef,
)

from ._openai_types import (
    OpenAIChoiceDict,
    OpenAIDeltaResponseDict,
    OpenAIRequestBodyDict,
    OpenAIResponseBodyDict,
    OpenAIStreamChunkDict,
    OpenAITokenUsageDict,
    OpenAIToolCallDict,
    OpenAIToolDict,
)
from .base import ProtocolAdapter

# Sentinel: typed empty dict fallbacks so choices[0] won't produce Unknown types
_EMPTY_CHOICE: OpenAIChoiceDict = {}
_EMPTY_DELTA: OpenAIDeltaResponseDict = {}


class OpenAIAdapter(ProtocolAdapter):
    """Adapter for OpenAI-compatible APIs (chat completions, completions)."""

    provider: str = "openai"

    def can_handle(self, request: Request) -> bool:  # type: ignore[override,unused-ignore]
        """Match OpenAI requests by path and Authorization header."""
        path = request.url.path
        if path in ("/v1/chat/completions", "/v1/completions"):
            return True
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer sk-"):
            return True
        return False

    async def normalize_request(  # type: ignore[override,unused-ignore]
        self, raw_body: dict[str, Any], headers: dict[str, str], path: str
    ) -> NormalizedRequest:
        """Convert OpenAI chat completions body to NormalizedRequest."""
        # Double-cast through object is the documented pyright pattern for
        # narrowing dict[str, Any] to a specific TypedDict through a class boundary.
        body = cast(OpenAIRequestBodyDict, cast(object, raw_body))

        messages = [
            Message(
                role=m.get("role", "user"),
                content=m.get("content", ""),
                name=m.get("name"),
            )
            for m in body.get("messages", [])
        ]

        tools_raw = body.get("tools") or body.get("functions")
        tools: list[ToolDef] | None = None
        if tools_raw:
            typed_tools = cast(list[OpenAIToolDict], tools_raw)  # type: ignore[redundant-cast]
            tools = [
                ToolDef(
                    name=t.get("function", {}).get("name", t.get("name", "")),
                    description=t.get("function", {}).get("description", t.get("description", "")),
                    parameters=t.get("function", {}).get("parameters") or t.get("parameters"),
                )
                for t in typed_tools
            ]

        return NormalizedRequest(
            provider="openai",
            model=body.get("model", ""),
            messages=messages,
            tools=tools,
            stream=body.get("stream", False),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            raw_body=raw_body,
        )

    def normalize_response(self, raw_body: dict[str, Any]) -> NormalizedResponse:  # type: ignore[override,unused-ignore]
        """Convert OpenAI chat completions response to NormalizedResponse."""
        body = cast(OpenAIResponseBodyDict, cast(object, raw_body))

        choices = body.get("choices", [])
        choice: OpenAIChoiceDict = choices[0] if choices else _EMPTY_CHOICE
        message = choice.get("message") or choice.get("delta") or _EMPTY_DELTA

        content = message.get("content") or message.get("reasoning_content")
        tool_calls: list[ToolCall] | None = None
        raw_tool_calls = message.get("tool_calls") or []
        if raw_tool_calls:
            typed_tcs = cast(list[OpenAIToolCallDict], raw_tool_calls)
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("function", {}).get("name", ""),
                    arguments=_extract_arguments(tc),
                )
                for tc in typed_tcs
            ]

        usage: TokenUsage | None = None
        usage_raw = body.get("usage") or {}
        if usage_raw:
            typed_usage = cast(OpenAITokenUsageDict, usage_raw)
            usage = TokenUsage(
                prompt_tokens=typed_usage.get("prompt_tokens", 0),
                completion_tokens=typed_usage.get("completion_tokens", 0),
                total_tokens=typed_usage.get("total_tokens", 0),
            )

        return NormalizedResponse(
            provider="openai",
            model=body.get("model", ""),
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            raw_body=raw_body,
        )

    def extract_stream_chunk(self, sse_data: str) -> StreamChunk | None:
        """Parse an SSE data line from OpenAI streaming response."""
        data = sse_data.strip()
        if not data:
            return None
        if data == "[DONE]":
            return StreamChunk(is_done=True)

        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return None

        chunk = cast(OpenAIStreamChunkDict, obj)

        choices = chunk.get("choices", [])
        choice: OpenAIChoiceDict = choices[0] if choices else _EMPTY_CHOICE
        delta = choice.get("delta") or _EMPTY_DELTA

        tool_call_delta: dict[str, object] | None = None
        tool_calls_raw = delta.get("tool_calls")
        if tool_calls_raw:
            first = tool_calls_raw[0] if tool_calls_raw else None
            tool_call_delta = dict(first) if first is not None else None

        usage: TokenUsage | None = None
        usage_raw = chunk.get("usage") or {}
        if usage_raw:
            typed_usage = cast(OpenAITokenUsageDict, usage_raw)
            usage = TokenUsage(
                prompt_tokens=typed_usage.get("prompt_tokens", 0),
                completion_tokens=typed_usage.get("completion_tokens", 0),
                total_tokens=typed_usage.get("total_tokens", 0),
            )

        return StreamChunk(
            delta_content=delta.get("content") or delta.get("reasoning_content"),
            delta_tool_call=tool_call_delta,
            usage=usage,
            finish_reason=choice.get("finish_reason"),
            is_done=False,
            raw_data=dict(chunk),
        )

    def get_upstream_url(self, path: str, base_url: str) -> str:  # type: ignore[override,unused-ignore]
        """Build the full upstream URL, avoiding double /v1."""
        base = base_url.rstrip("/")
        normalized_path = path
        if base.endswith("/v1") and path.startswith("/v1"):
            normalized_path = path[3:]
        return base + normalized_path

    def get_upstream_headers(  # type: ignore[override,unused-ignore]
        self, original_headers: dict[str, str], api_key: str, base_url: str = ""
    ) -> dict[str, str]:
        """Preserve headers but replace Authorization with real API key.

        Host header is derived from base_url to support multiple upstream providers.
        If no gateway-level API key is configured, preserve the client's Authorization header.
        """
        headers = {
            k: v for k, v in original_headers.items() if k.lower() not in ("host", "authorization", "transfer-encoding")
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            # Preserve client's Authorization when no gateway-level key is configured
            auth = original_headers.get("Authorization") or original_headers.get("authorization")
            if auth:
                headers["Authorization"] = auth
        # Derive Host from base_url (e.g. "https://api.deepseek.com" -> "api.deepseek.com")
        if base_url:
            from urllib.parse import urlparse

            host = urlparse(base_url).netloc
            if host:
                headers["Host"] = host
        else:
            headers["Host"] = "api.openai.com"
        return headers


def _extract_arguments(tc: OpenAIToolCallDict) -> dict[str, object]:
    """Safely extract tool call arguments as a dict, handling both str and dict forms."""
    func = tc.get("function") or {}
    raw_args = func.get("arguments", {})  # type: ignore[reportUnknownMemberType]
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
        result: dict[str, object] = cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}
        return result
    return raw_args  # type: ignore[return-value]
