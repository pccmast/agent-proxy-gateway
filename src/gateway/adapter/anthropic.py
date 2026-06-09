"""Anthropic protocol adapter — maps Anthropic Messages API to NormalizedRequest/Response."""

import json
from typing import Any, cast

from fastapi import Request

from shared.models import (
    Message,
    NormalizedRequest,
    NormalizedResponse,
    TokenUsage,
    StreamChunk,
    ToolDef,
    ToolCall,
)
from .base import ProtocolAdapter

# Sentinel
_EMPTY_DICT: dict[str, Any] = {}


class AnthropicAdapter(ProtocolAdapter):
    """Adapter for Anthropic Messages API (/v1/messages)."""

    provider: str = "anthropic"

    def can_handle(self, request: Request) -> bool:  # type: ignore[override,unused-ignore]
        """Match Anthropic requests by path or x-api-key header."""
        path = request.url.path
        if path == "/v1/messages":
            return True
        # Anthropic uses x-api-key, not Authorization
        api_key = request.headers.get("x-api-key", "")
        if api_key.startswith("sk-ant-"):
            return True
        return False

    async def normalize_request(
        self, raw_body: dict[str, Any], headers: dict[str, str], path: str
    ) -> NormalizedRequest:
        """Convert Anthropic Messages body to NormalizedRequest.

        Key difference: Anthropic has system as a top-level field, not a message.
        """
        body: dict[str, Any] = raw_body

        # System prompt is top-level in Anthropic
        system_prompt = body.get("system", "")
        messages_raw = body.get("messages", [])

        # Build unified message list
        messages: list[Message] = []
        if system_prompt:
            if isinstance(system_prompt, str):
                messages.append(Message(role="system", content=system_prompt))
            elif isinstance(system_prompt, list):
                # Anthropic supports system as structured content blocks
                text_parts = [b.get("text", "") for b in system_prompt if isinstance(b, dict) and b.get("type") == "text"]
                messages.append(Message(role="system", content=" ".join(text_parts)))

        for m in messages_raw:
            role = m.get("role", "user")
            content = ""
            raw_content = m.get("content", "")
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                # Structured content blocks — extract text
                parts = [b.get("text", "") for b in raw_content if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(parts)
            messages.append(Message(role=role, content=content))

        # Tools
        tools_raw = body.get("tools")
        tools = None
        if tools_raw:
            tools = [
                ToolDef(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    parameters=t.get("input_schema") or {},
                )
                for t in tools_raw
            ]

        return NormalizedRequest(
            provider="anthropic",
            model=body.get("model", ""),
            messages=messages,
            tools=tools,
            stream=body.get("stream", False),
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            raw_body=raw_body,
        )

    def normalize_response(self, raw_body: dict[str, Any]) -> NormalizedResponse:
        """Convert Anthropic Messages response to NormalizedResponse."""
        body: dict[str, Any] = raw_body

        # Extract text content
        content_blocks = body.get("content", [])
        text_parts: list[str] = []
        tool_calls: list[ToolCall] | None = None
        tool_uses: list[dict[str, Any]] = []

        for block in content_blocks if isinstance(content_blocks, list) else []:
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                tool_uses.append(block)

        if tool_uses:
            tool_calls = [
                ToolCall(
                    id=tu.get("id", ""),
                    name=tu.get("name", ""),
                    arguments=tu.get("input", {}),
                )
                for tu in tool_uses
            ]

        usage = None
        usage_raw = body.get("usage") or {}
        if usage_raw:
            input_tokens = int(usage_raw.get("input_tokens", 0))
            output_tokens = int(usage_raw.get("output_tokens", 0))
            usage = TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )

        return NormalizedResponse(
            provider="anthropic",
            model=body.get("model", ""),
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=body.get("stop_reason"),
            raw_body=raw_body,
        )

    def extract_stream_chunk(self, sse_data: str) -> StreamChunk | None:
        """Parse an SSE data line from Anthropic streaming response.

        Anthropic SSE events:
          data: {"type": "content_block_delta", "delta": {"text": "Hello"}}
          data: {"type": "message_delta", "usage": {...}}
          data: {"type": "message_stop"}
        """
        data = sse_data.strip()
        if not data:
            return None
        if data == "[DONE]":
            return StreamChunk(is_done=True)

        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return None

        event_type = obj.get("type", "")
        delta_content: str | None = None
        delta_tool_call: dict[str, Any] | None = None
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        is_done = False

        if event_type == "content_block_delta":
            delta = obj.get("delta", _EMPTY_DICT)
            if delta.get("type") == "text_delta":
                delta_content = delta.get("text")
            elif delta.get("type") == "input_json_delta":
                delta_tool_call = {"partial_json": delta.get("partial_json", "")}

        elif event_type == "content_block_start":
            block = obj.get("content_block", _EMPTY_DICT)
            if block.get("type") == "tool_use":
                delta_tool_call = {"name": block.get("name", ""), "id": block.get("id", "")}

        elif event_type == "message_delta":
            usage_raw = obj.get("usage") or {}
            if usage_raw:
                usage = TokenUsage(
                    prompt_tokens=int(usage_raw.get("input_tokens", 0)),
                    completion_tokens=int(usage_raw.get("output_tokens", 0)),
                    total_tokens=0,
                )
            stop_reason = obj.get("delta", _EMPTY_DICT).get("stop_reason")
            if stop_reason:
                finish_reason = stop_reason

        elif event_type == "message_stop":
            is_done = True

        return StreamChunk(
            delta_content=delta_content,
            delta_tool_call=delta_tool_call,
            usage=usage,
            finish_reason=finish_reason,
            is_done=is_done,
            raw_data=obj,
        )

    def get_upstream_url(self, path: str, base_url: str) -> str:
        """Build the Anthropic upstream URL."""
        base = base_url.rstrip("/")
        if not base.endswith("/v1") and not path.startswith("/v1"):
            return base + path
        return base + path

    def get_upstream_headers(self, original_headers: dict[str, str], api_key: str) -> dict[str, str]:
        """Replace auth headers with Anthropic's x-api-key format."""
        skip = {"host", "x-api-key", "authorization", "transfer-encoding"}
        headers = {k: v for k, v in original_headers.items() if k.lower() not in skip}
        headers["x-api-key"] = api_key
        headers["Host"] = "api.anthropic.com"
        headers["anthropic-version"] = original_headers.get("anthropic-version", "2023-06-01")
        return headers
