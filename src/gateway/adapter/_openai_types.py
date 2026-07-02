"""Type-safe TypedDict definitions for OpenAI API request/response JSON.

These provide precise types for JSON parsing so that basedpyright
doesn't see every .get() as returning Any.
"""

from typing import NotRequired, TypedDict


class OpenAIMessageDict(TypedDict, total=False):
    """An OpenAI chat message in the messages array."""

    role: str
    content: str
    name: NotRequired[str]


class OpenAIFunctionCallDict(TypedDict, total=False):
    """Function call inside a tool call."""

    name: str
    arguments: str


class OpenAIToolCallDict(TypedDict, total=False):
    """A tool call (or partial) from assistant message or delta."""

    id: str
    type: str
    function: OpenAIFunctionCallDict


class OpenAIFunctionDefDict(TypedDict, total=False):
    """A function definition inside a tool definition."""

    name: str
    description: str
    parameters: dict[str, object]


class OpenAIToolDict(TypedDict, total=False):
    """A tool definition in the request."""

    type: str
    function: OpenAIFunctionDefDict


class OpenAIRequestBodyDict(TypedDict, total=False):
    """The body of a POST /v1/chat/completions request."""

    model: str
    messages: list[OpenAIMessageDict]
    stream: bool
    temperature: float
    max_tokens: int
    tools: list[OpenAIToolDict]
    functions: list[OpenAIToolDict]  # legacy


class OpenAIMessageResponseDict(TypedDict, total=False):
    """The message field inside a non-streaming choice."""

    role: str
    content: str
    tool_calls: list[OpenAIToolCallDict]


class OpenAIDeltaResponseDict(TypedDict, total=False):
    """The delta field inside a streaming choice."""

    role: str
    content: str
    tool_calls: list[OpenAIToolCallDict]


class OpenAIChoiceDict(TypedDict, total=False):
    """A single choice in either streaming or non-streaming response."""

    index: int
    message: OpenAIMessageResponseDict
    delta: OpenAIDeltaResponseDict
    finish_reason: str


class OpenAITokenUsageDict(TypedDict):
    """Token usage stats."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class OpenAIResponseBodyDict(TypedDict, total=False):
    """A non-streaming /v1/chat/completions response."""

    id: str
    object: str
    model: str
    choices: list[OpenAIChoiceDict]
    usage: OpenAITokenUsageDict


class OpenAIStreamChunkDict(TypedDict, total=False):
    """A single chunk in the SSE stream (parsed from 'data: {...}')."""

    id: str
    object: str
    model: str
    choices: list[OpenAIChoiceDict]
    usage: NotRequired[OpenAITokenUsageDict]
