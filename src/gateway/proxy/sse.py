"""SSE StreamInterceptor — intercepts, parses, and processes SSE streaming chunks.

Parses the SSE wire format (data: {...}\n\n), extracts individual chunks,
runs them through the middleware chain, and re-emits them downstream.
v2 — added TTFT (Time to First Token) tracking.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from shared.models import StreamContext, TokenUsage


class SSEInterceptor:
    """Intercepts an SSE stream from upstream, processes each chunk through middleware, and re-emits.

    Handles:
    - Parsing SSE format: "data: {json}\n\n"
    - Detecting [DONE] signal
    - Accumulating content for downstream chunk-level guardrails
    - Aggregating token usage from the final chunk
    - Calling middleware chain on each chunk
    - Finalizing trace span on stream close
    - Tracking TTFT (Time to First Token) for streaming performance metrics
    """

    def __init__(
        self,
        adapter: Any,
        middleware_chain: Any,
        stream_context: Any,  # RequestContext-like (has trace_id, span_id, request)
        trace_engine: Any = None,
        circuit_breaker: Any = None,
    ):
        self.adapter = adapter
        self.middleware_chain = middleware_chain
        self.trace_context = stream_context
        self.trace_engine = trace_engine
        self.circuit_breaker = circuit_breaker

        # Accumulation state
        self.accumulated_content = ""
        self.accumulated_tool_calls: list[dict[str, Any]] = []
        self.total_usage: TokenUsage | None = None
        self.finish_reason: str | None = None
        self.final_chunk_raw: dict[str, Any] = {}
        self.guard_results: list[Any] = []
        self.stream_done = False
        self.current_buffer = ""

        # TTFT tracking
        self._stream_start_time: float = time.monotonic()
        self._first_token_time: float | None = None
        self._ttft_ms: float = 0.0

    async def aiter_lines(self, response: Any) -> AsyncIterator[tuple[bytes, str]]:
        """Iterate over SSE lines from the upstream response.

        Yields (raw_bytes_line, decoded_line) tuples.
        Handles SSE line-by-line parsing.
        """
        async for chunk_bytes in response.aiter_bytes():
            self.current_buffer += chunk_bytes.decode("utf-8", errors="replace")
            while "\n" in self.current_buffer:
                line, self.current_buffer = self.current_buffer.split("\n", 1)
                line_stripped = line.rstrip("\r")
                yield (line_stripped.encode("utf-8") + b"\n", line_stripped)

    async def process_line(self, line_bytes: bytes, line_str: str) -> bytes | None:
        """Process a single SSE line.

        Returns:
            bytes to re-emit downstream, or None to skip.
        """
        # Pass through non-data lines (comments, empty lines)
        if not line_str.strip():
            return line_bytes

        if not line_str.startswith("data:"):
            return line_bytes

        data_str = line_str[5:].lstrip()

        # Parse the data payload
        if data_str == "[DONE]":
            self.stream_done = True
            # Pass [DONE] through
            return line_bytes

        try:
            _chunk_data = json.loads(data_str)
        except json.JSONDecodeError:
            return line_bytes

        # Extract chunk via adapter
        chunk = self.adapter.extract_stream_chunk(data_str)
        if chunk is None:
            return line_bytes

        # Accumulate content
        if chunk.delta_content:
            # Record TTFT on first content-bearing chunk
            if self._first_token_time is None:
                self._first_token_time = time.monotonic()
                self._ttft_ms = (self._first_token_time - self._stream_start_time) * 1000.0
            self.accumulated_content += chunk.delta_content
        if chunk.delta_tool_call:
            self.accumulated_tool_calls.append(chunk.delta_tool_call)
        if chunk.usage:
            self.total_usage = chunk.usage
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason
        if chunk.raw_data:
            self.final_chunk_raw = chunk.raw_data

        # Run middleware chain on this chunk
        stream_ctx = StreamContext(
            trace_id=self.trace_context.trace_id,
            span_id=self.trace_context.span_id,
            request=self.trace_context.request,
            accumulated_content=self.accumulated_content,
            guard_results=self.guard_results,
        )

        result = await self.middleware_chain.run_stream_chunk(chunk, stream_ctx)
        self.guard_results = stream_ctx.guard_results

        if result is None:
            # Replace blocked content with a placeholder instead of
            # silently dropping the chunk. Dropping causes client-side
            # garbled text (e.g. "北京是" + [drop] + "的首都" = "北京是的首都").
            # Replacing preserves stream structure while filtering harm.
            if chunk.delta_content:
                placeholder = "[已过滤]"
                replacement = 'data: {"choices":[{"delta":{"content":"' + placeholder + '"}}]}\n\n'
                return replacement.encode("utf-8")
            # Tool-call / usage-only chunks: drop silently.
            # These carry structural metadata, not user-visible text.
            return None

        # Re-emit the chunk (pass original bytes through for minimal latency)
        return line_bytes

    async def finalize(self) -> list[bytes]:
        """Called after stream ends — normal or abnormal.

        Yields any final bytes needed (e.g., trailing newline).
        On abnormal completion (stream_done=False, e.g. client disconnect),
        records the trace with status=\"abandoned\".
        """
        # Determine completion mode
        completed_normally = self.stream_done
        status = "ok" if completed_normally else "abandoned"

        # Circuit breaker: stream completed normally — upstream is healthy
        if completed_normally and self.circuit_breaker:
            self.circuit_breaker.record_success()

        # Build accumulated response for trace recording
        accumulated_content = self.accumulated_content
        if self.trace_engine and self.trace_context.trace_id:
            from shared.models import (
                EvalScoreRecord,
                GuardHitRecord,
                NormalizedResponse,
                ResponseContext,
                SpanFinishParams,
            )

            normalized_resp = NormalizedResponse(
                provider=self.trace_context.provider,
                model=self.trace_context.request.model,
                content=accumulated_content or None,
                usage=self.total_usage,
                finish_reason=self.finish_reason,
                raw_body=self.final_chunk_raw,
            )

            # Merge request-phase guard_results (from run_request before streaming)
            # with stream-phase guard_results accumulated during chunk processing.
            all_guard = list(self.trace_context.guard_results) + self.guard_results

            resp_ctx = ResponseContext(
                trace_id=self.trace_context.trace_id,
                span_id=self.trace_context.span_id,
                request=self.trace_context.request,
                response=normalized_resp,
                guard_results=all_guard,
            )

            # Run middleware response phase
            try:
                resp_ctx = await self.middleware_chain.run_response(resp_ctx)
            except Exception:
                pass

            await self.trace_engine.finish_span(
                SpanFinishParams(
                    trace_id=self.trace_context.trace_id,
                    span_id=self.trace_context.span_id,
                    status=status,
                    token_usage=self.total_usage,
                    ttft_ms=self._ttft_ms,
                    finish_reason=self.finish_reason,
                    guard_hits=[
                        GuardHitRecord(
                            rule_id=g.rule_id,
                            action=g.action.value,
                            matches=g.matches,
                            confidence=g.confidence,
                            details=g.details,
                        )
                        for g in resp_ctx.guard_results
                    ],
                    eval_scores=[
                        EvalScoreRecord(name=r.name, score=r.score, details=r.details) for r in resp_ctx.eval_results
                    ],
                    request_body=self.trace_context.request.raw_body,
                    response_body=self.final_chunk_raw,
                )
            )

        return []
