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

from typing import Any

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from shared.config import GatewaySettings
from shared.logging import get_logger
from .sse import SSEInterceptor
from .middleware import MiddlewareChain, BlockException

logger = get_logger()


class ProxyEngine:
    """Core proxy engine that routes incoming requests through the middleware chain and forwards to upstream."""

    def __init__(
        self,
        settings: GatewaySettings,
        adapter_registry: Any,  # AdapterRegistry
        middleware_chain: MiddlewareChain | None = None,
        trace_engine: Any = None,  # TraceEngine
    ):
        self.settings = settings
        self.adapter_registry = adapter_registry
        self.middleware_chain = middleware_chain or MiddlewareChain()
        self.trace_engine = trace_engine
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
            raw_body = {}

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
        try:
            ctx = await self.middleware_chain.run_request(ctx)
        except BlockException as exc:
            if self.trace_engine and trace_id:
                await self.trace_engine.finish_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    status="blocked",
                    guard_hits=[exc.rule_id],
                )
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.reason, "blocked_by": exc.rule_id},
            )

        # --- Phase 3: Forward to upstream ---
        api_key = self._get_api_key(adapter.provider)
        upstream_url = adapter.get_upstream_url(
            path, self._get_base_url(adapter.provider)
        )
        upstream_headers = adapter.get_upstream_headers(headers, api_key)

        logger.info(
            "forwarding_request",
            provider=adapter.provider,
            model=normalized_req.model,
            upstream_url=upstream_url,
            stream=normalized_req.stream,
            trace_id=trace_id,
        )

        try:
            client = await self._get_client()
            if normalized_req.stream:
                return await self._forward_stream(
                    client, upstream_url, upstream_headers, raw_body, adapter, ctx
                )
            else:
                return await self._forward_non_stream(
                    client, upstream_url, upstream_headers, raw_body, adapter, ctx
                )
        except httpx.TimeoutException:
            logger.error("upstream_timeout", upstream_url=upstream_url, trace_id=trace_id)
            if self.trace_engine and trace_id:
                await self.trace_engine.finish_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    status="timeout",
                )
            return JSONResponse(
                status_code=504,
                content={"error": "Upstream request timed out"},
            )
        except httpx.ConnectError as e:
            logger.error("upstream_connect_error", upstream_url=upstream_url, error=str(e))
            if self.trace_engine and trace_id:
                await self.trace_engine.finish_span(
                    trace_id=trace_id,
                    span_id=span_id,
                    status="error",
                )
            return JSONResponse(
                status_code=502,
                content={"error": f"Cannot connect to upstream: {str(e)}"},
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
        )

        # Middleware chain (response)
        try:
            resp_ctx = await self.middleware_chain.run_response(resp_ctx)
        except BlockException:
            pass  # Don't block output after it's already generated; just log

        # Finish trace
        if self.trace_engine and ctx.trace_id:
            await self.trace_engine.finish_span(
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                token_usage=normalized_resp.usage,
                status="ok",
                eval_scores={r.name: r.score for r in resp_ctx.eval_results},
                guard_hits=[g.rule_id for g in resp_ctx.guard_results],
            )

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
        )

        async def generate():
            async with client.stream("POST", url, json=body, headers=headers) as response:
                async for line_bytes, line_str in sse_interceptor.aiter_lines(response):
                    result_bytes = await sse_interceptor.process_line(line_bytes, line_str)
                    if result_bytes is not None:
                        yield result_bytes

            final_bytes = await sse_interceptor.finalize()
            for fb in final_bytes:
                yield fb

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
        """Get API key for a provider from settings."""
        if provider == "openai":
            return self.settings.openai_api_key
        elif provider == "anthropic":
            return self.settings.anthropic_api_key
        return ""

    def _get_base_url(self, provider: str) -> str:
        """Get base URL for a provider from settings."""
        if provider == "openai":
            return "https://api.openai.com"
        elif provider == "anthropic":
            return "https://api.anthropic.com"
        return ""

    async def close(self) -> None:
        """Clean up resources."""
        if self._client:
            await self._client.aclose()
            self._client = None
