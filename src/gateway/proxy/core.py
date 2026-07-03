"""ProxyEngine — transparent HTTP proxy that intercepts Agent requests to LLM/Tool APIs.

Supports both non-streaming (JSON) and streaming (SSE) response forwarding.
"""

# pyright: reportAny=false, reportExplicitAny=false, reportUnannotatedClassAttribute=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnusedCallResult=false
# Rationale: ProxyEngine uses Any intentionally — adapter_registry/trace_engine are
# injected at startup with concrete types but stored via FastAPI app.state, which is
# inherently untyped. Full typing would require a custom Protocol or TypeVar chain
# that adds complexity without runtime benefit.

import asyncio
import os
from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from shared.config import GatewaySettings
from shared.logging import get_logger

from .middleware import BlockException, MiddlewareChain, RateLimitException
from .sse import SSEInterceptor

logger = get_logger()


class ProxyEngine:
    """Core proxy engine that routes incoming requests through the middleware chain and forwards to upstream."""

    def __init__(
        self,
        settings: GatewaySettings,
        adapter_registry: Any,  # AdapterRegistry
        middleware_chain: MiddlewareChain | None = None,
        trace_engine: Any = None,  # TraceEngine
        circuit_breaker: Any = None,  # CircuitBreaker
    ):
        self.settings = settings
        self.adapter_registry = adapter_registry
        self.middleware_chain = middleware_chain or MiddlewareChain()
        self.trace_engine = trace_engine
        self.circuit_breaker = circuit_breaker
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazily initialize httpx client with connection pooling."""
        if self._client is None:
            timeout = httpx.Timeout(
                connect=10.0,
                read=self.settings.upstream_timeout,
                write=30.0,
                pool=10.0,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                http2=True,
            )
        return self._client

    async def handle_request(self, request: Request) -> Response:
        """Main entry point — handle all incoming proxy requests.

        This is the FastAPI catch-all route handler.
        """
        # --- Phase 0: Resolve adapter ---
        adapter = self.adapter_registry.resolve(request)
        if adapter is None:
            logger.warning("no_adapter", path=request.url.path, method=request.method)
            return JSONResponse(
                status_code=404,
                content={"error": f"No adapter found for path: {request.url.path}"},
            )

        # --- Phase 1: Parse and normalize request ---
        try:
            raw_body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON in request body"},
            )

        headers = dict(request.headers)
        path = request.url.path

        from shared.models import RequestContext

        normalized_req = await adapter.normalize_request(raw_body, headers, path)

        # Get trace context from trace engine (if available)
        trace_id = ""
        span_id = ""
        if self.trace_engine:
            trace_id, span_id = await self.trace_engine.start_trace(request)

        ctx = RequestContext(
            trace_id=trace_id,
            span_id=span_id,
            request=normalized_req,
            headers=headers,
            path=path,
            provider=adapter.provider,
        )

        # --- Phase 2: Middleware chain (request) ---
        # --- Phase 3: Forward to upstream ---
        # Wrapped in outer try to guarantee finish_span on any unhandled exception
        try:
            try:
                ctx = await self.middleware_chain.run_request(ctx)
            except BlockException as exc:
                if self.trace_engine and trace_id:
                    from shared.models import GuardHitRecord, SpanFinishParams

                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="blocked",
                            guard_hits=[GuardHitRecord(rule_id=exc.rule_id, action="block")],
                            request_body=raw_body,
                        )
                    )
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"error": exc.reason, "blocked_by": exc.rule_id},
                )
            except RateLimitException as exc:
                if self.trace_engine and trace_id:
                    from shared.models import SpanFinishParams

                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="rate_limited",
                            error_message=exc.reason,
                            request_body=raw_body,
                        )
                    )
                return JSONResponse(
                    status_code=429,
                    content={
                        "error": exc.reason,
                        "retry_after_seconds": exc.retry_after,
                    },
                )

            # --- Circuit breaker check ---
            if self.circuit_breaker and not self.circuit_breaker.allow_request():
                logger.warning(
                    "circuit_breaker_open",
                    provider=adapter.provider,
                    trace_id=trace_id,
                )
                if self.trace_engine and trace_id:
                    from shared.models import SpanFinishParams

                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="error",
                            error_message="Circuit breaker is OPEN — upstream unreachable",
                            request_body=raw_body,
                        )
                    )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Service temporarily unavailable",
                        "detail": "Circuit breaker is open — upstream is unreachable",
                    },
                )

            # --- Propagate request-phase redactions to the upstream body ---
            # GuardrailsEngine REDACT modifies ctx.request.messages in-place,
            # but the upstream receives raw_body (the original request dict).
            # Build a new dict to avoid Content-Length mismatch on connection reuse.
            if ctx.guard_results and any(hasattr(g, "action") and str(g.action) == "redact" for g in ctx.guard_results):
                raw_body = {
                    **raw_body,
                    "messages": [{"role": m.role, "content": m.content} for m in ctx.request.messages],
                }

            api_key = self._get_api_key(adapter.provider)
            upstream_url = adapter.get_upstream_url(path, self._get_base_url(adapter.provider))
            upstream_headers = adapter.get_upstream_headers(headers, api_key, self._get_base_url(adapter.provider))

            logger.info(
                "forwarding_request",
                provider=adapter.provider,
                model=normalized_req.model,
                upstream_url=upstream_url,
                stream=normalized_req.stream,
                trace_id=trace_id,
                api_key_prefix=api_key[:10] if api_key else "empty",
            )

            try:
                # Apply full-link timeout if RequestTimeoutGuard (P3) set a deadline.
                # Compute remaining budget and enforce via asyncio.wait_for on the
                # forward+processing coroutine. Non-stream and stream paths are
                # handled identically — wait_for wraps the entire forward.
                remaining = (
                    ctx.timeout_deadline - asyncio.get_running_loop().time() if ctx.timeout_deadline > 0 else None
                )
                if remaining is not None and remaining <= 0:
                    raise TimeoutError()

                client = await self._get_client()
                if normalized_req.stream:
                    forward_coro = self._forward_stream(client, upstream_url, upstream_headers, raw_body, adapter, ctx)
                else:
                    forward_coro = self._forward_non_stream(
                        client, upstream_url, upstream_headers, raw_body, adapter, ctx
                    )

                if remaining is not None:
                    return await asyncio.wait_for(forward_coro, timeout=remaining)
                return await forward_coro
            except httpx.TimeoutException:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                    self._record_cb_failure()
                logger.error("upstream_timeout", upstream_url=upstream_url, trace_id=trace_id)
                if self.trace_engine and trace_id:
                    from shared.models import SpanFinishParams

                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="timeout",
                            error_message=f"Upstream request timed out after {self.settings.upstream_timeout}s",
                            request_body=raw_body,
                            upstream_url=upstream_url,
                        )
                    )
                return JSONResponse(
                    status_code=504,
                    content={"error": "Upstream request timed out"},
                )
            except httpx.ConnectError as e:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                    self._record_cb_failure()
                logger.error("upstream_connect_error", upstream_url=upstream_url, error=str(e))
                if self.trace_engine and trace_id:
                    from shared.models import SpanFinishParams

                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="error",
                            error_message=f"Cannot connect to upstream: {str(e)}",
                            request_body=raw_body,
                            upstream_url=upstream_url,
                        )
                    )
                return JSONResponse(
                    status_code=502,
                    content={"error": f"Cannot connect to upstream: {str(e)}"},
                )

        except TimeoutError:
            # Full-link timeout enforced by RequestTimeoutGuard (P3)
            remaining = 0.0
            if ctx.timeout_deadline > 0:
                remaining = max(0.0, ctx.timeout_deadline - asyncio.get_running_loop().time())
            logger.error(
                "request_timeout",
                trace_id=trace_id,
                timeout_seconds=ctx.timeout_seconds,
                remaining=round(remaining, 2),
            )
            if self.trace_engine and trace_id:
                from shared.models import SpanFinishParams

                await self.trace_engine.finish_span(
                    SpanFinishParams(
                        trace_id=trace_id,
                        span_id=span_id,
                        status="timeout",
                        error_message=f"Request timed out after {ctx.timeout_seconds:.0f}s (P3 full-link guard)",
                        request_body=raw_body,
                    )
                )
            return JSONResponse(
                status_code=504,
                content={
                    "error": f"Gateway request timeout ({ctx.timeout_seconds:.0f}s)",
                },
            )

        except Exception as exc:
            # Catch-all: prevent orphan traces from unhandled exceptions
            import traceback as _tb2

            _tb_str = _tb2.format_exc()
            logger.error(
                "unhandled_exception",
                trace_id=trace_id,
                span_id=span_id,
                error=str(exc),
                traceback=_tb_str,
            )
            if self.trace_engine and trace_id:
                from shared.models import SpanFinishParams

                try:
                    await self.trace_engine.finish_span(
                        SpanFinishParams(
                            trace_id=trace_id,
                            span_id=span_id,
                            status="error",
                            error_message=f"Unhandled exception: {str(exc)}",
                            request_body=raw_body,
                        )
                    )
                except Exception as finish_err:
                    logger.error(
                        "finish_span_failed_in_exception_handler",
                        trace_id=trace_id,
                        error=str(finish_err),
                    )
            return JSONResponse(
                status_code=500,
                content={
                    "error": f"Internal gateway error: {type(exc).__name__}",
                    "detail": str(exc)[:500],
                },
            )

    async def _forward_non_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        adapter: Any,
        ctx: Any,
    ) -> Response:
        """Forward a non-streaming request and return JSON response."""
        response = await client.post(url, json=body, headers=headers)

        # Upstream returned a server error — the request failed at the LLM side.
        # Record the failure for circuit-breaking and prevent the gateway
        # from treating this as a successful forward.
        if response.status_code >= 500:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
                self._record_cb_failure()
            if self.trace_engine and ctx.trace_id:
                from shared.models import SpanFinishParams

                await self.trace_engine.finish_span(
                    SpanFinishParams(
                        trace_id=ctx.trace_id,
                        span_id=ctx.span_id,
                        status="error",
                        error_message=f"Upstream returned {response.status_code}",
                        upstream_url=url,
                        request_body=body,
                    )
                )
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"Upstream returned {response.status_code}",
                    "detail": response.text[:500] if response.text else "",
                },
            )

        try:
            raw_resp = response.json()
        except Exception:
            raw_resp = {}

        normalized_resp = adapter.normalize_response(raw_resp)

        from shared.models import ResponseContext

        resp_ctx = ResponseContext(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            request=ctx.request,
            response=normalized_resp,
            guard_results=list(ctx.guard_results),  # preserve request-phase hits
        )

        # Middleware chain (response)
        try:
            resp_ctx = await self.middleware_chain.run_response(resp_ctx)
        except BlockException:
            pass  # Don't block output after it's already generated; just log

        # Finish trace
        if self.trace_engine and ctx.trace_id:
            from shared.models import (
                EvalScoreRecord,
                GuardHitRecord,
                SpanFinishParams,
            )

            await self.trace_engine.finish_span(
                SpanFinishParams(
                    trace_id=ctx.trace_id,
                    span_id=ctx.span_id,
                    status="ok",
                    token_usage=normalized_resp.usage,
                    finish_reason=normalized_resp.finish_reason,
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
                    request_body=ctx.request.raw_body if ctx.request else None,
                    response_body=normalized_resp.raw_body,
                    tool_calls=normalized_resp.tool_calls,
                    temperature=ctx.request.temperature if ctx.request else None,
                    max_tokens=ctx.request.max_tokens if ctx.request else None,
                    upstream_url=url,
                )
            )

        # Circuit breaker: non-stream response received successfully
        if self.circuit_breaker:
            self.circuit_breaker.record_success()
            self._update_cb_gauge()

        return JSONResponse(
            content=raw_resp,
            status_code=response.status_code,
        )

    async def _forward_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        adapter: Any,
        ctx: Any,
    ) -> StreamingResponse:
        """Forward a streaming request and return SSE StreamingResponse."""
        sse_interceptor = SSEInterceptor(
            adapter=adapter,
            middleware_chain=self.middleware_chain,
            stream_context=ctx,
            trace_engine=self.trace_engine,
            circuit_breaker=self.circuit_breaker,
        )

        async def generate():
            try:
                async with client.stream("POST", url, json=body, headers=headers) as response:
                    async for line_bytes, line_str in sse_interceptor.aiter_lines(response):
                        result_bytes = await sse_interceptor.process_line(line_bytes, line_str)
                        if result_bytes is not None:
                            yield result_bytes
            finally:
                # Guarantee finalize() runs even on client disconnect or
                # upstream stream error — prevents orphan traces in SSE mode.
                await sse_interceptor.finalize()

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def _get_api_key(self, provider: str) -> str:
        """Get API key for a provider from settings (dynamic lookup)."""
        key = self.settings.get_api_key(provider)
        env_val = os.environ.get("LM_STUDIO_API_KEY", "NOT_SET")
        logger.info("api_key_lookup", provider=provider, key_prefix=(key[:15] if key else "EMPTY"), env_lm=env_val[:15])
        return key

    def _get_base_url(self, provider: str) -> str:
        """Get base URL for a provider from YAML config via settings."""
        return self.settings.get_base_url(provider)

    async def close(self) -> None:
        """Clean up resources."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _update_cb_gauge() -> None:
        """Update the circuit-breaker Prometheus gauge (no-op if prometheus not available)."""
        try:
            from gateway.metrics import circuit_breaker_state as cb_gauge

            # We don't have provider context here, use "default"
            cb_gauge.labels(provider="default").set(0)
        except Exception:
            pass

    @staticmethod
    def _record_cb_failure() -> None:
        """Record a circuit-breaker failure and update the Prometheus gauge."""
        try:
            from gateway.metrics import circuit_breaker_state as cb_gauge

            cb_gauge.labels(provider="default").set(1)
        except Exception:
            pass
