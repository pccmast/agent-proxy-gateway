"""Tests for MiddlewareChain — exception propagation and ordering.

Covers the three-phase execution model (request / response / stream_chunk) and
the distinct exception-handling behaviour in each phase.
"""

import pytest

from gateway.proxy.middleware import (
    BlockException,
    Middleware,
    MiddlewareChain,
    RateLimitException,
)
from shared.models import (
    Message,
    NormalizedRequest,
    NormalizedResponse,
    RequestContext,
    ResponseContext,
    StreamChunk,
    StreamContext,
)

# ------------------------------------------------------------------ helpers


class _CountingMiddleware(Middleware):
    """Records call order + raises on demand — used for chain-order tests."""

    priority: int = 100
    instance_id: str = "A"

    def __init__(
        self,
        *,
        instance_id: str = "A",
        priority: int = 100,
        raise_on_request: type[Exception] | None = None,
        raise_on_response: type[Exception] | None = None,
        drop_stream_chunk: bool = False,
        raise_on_chunk: type[Exception] | None = None,
    ) -> None:
        super().__init__()
        self.priority = priority
        self.instance_id = instance_id
        self.calls: list[str] = []

        self._raise_on_request = raise_on_request
        self._raise_on_response = raise_on_response
        self._drop_stream_chunk = drop_stream_chunk
        self._raise_on_chunk = raise_on_chunk

    async def on_request(self, ctx: RequestContext) -> RequestContext:
        self.calls.append(f"req:{self.instance_id}")
        if self._raise_on_request:
            raise self._raise_on_request("test-reason")
        return ctx

    async def on_response(self, ctx: ResponseContext) -> ResponseContext:
        self.calls.append(f"resp:{self.instance_id}")
        if self._raise_on_response:
            raise self._raise_on_response("test-reason")
        return ctx

    async def on_stream_chunk(self, chunk: StreamChunk, ctx: StreamContext) -> StreamChunk | None:
        self.calls.append(f"chk:{self.instance_id}")
        if self._raise_on_chunk:
            raise self._raise_on_chunk("test-reason")
        if self._drop_stream_chunk:
            return None
        return chunk


def _make_req_ctx(trace_id: str = "t1", span_id: str = "s1") -> RequestContext:
    return RequestContext(
        trace_id=trace_id,
        span_id=span_id,
        request=NormalizedRequest(provider="test", model="m", messages=[Message(role="user", content="hi")]),
    )


def _make_resp_ctx() -> ResponseContext:
    return ResponseContext(
        trace_id="t1",
        span_id="s1",
        request=NormalizedRequest(provider="test", model="m", messages=[Message(role="user", content="hi")]),
        response=NormalizedResponse(provider="test", model="m", content="ok"),
    )


def _make_stream_ctx() -> StreamContext:
    return StreamContext(
        trace_id="t1",
        span_id="s1",
        request=NormalizedRequest(provider="test", model="m", messages=[Message(role="user", content="hi")]),
        accumulated_content="hello",
    )


# ------------------------------------------------------------------ Ordering


class TestMiddlewareOrdering:
    def test_runs_in_priority_order(self):
        """Middleware with lower priority runs first."""
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=100)
        mw_b = _CountingMiddleware(instance_id="B", priority=50)
        mw_c = _CountingMiddleware(instance_id="C", priority=200)
        chain.add_all([mw_c, mw_a, mw_b])

        assert [m.instance_id for m in chain.middlewares] == ["B", "A", "C"]

    @pytest.mark.asyncio
    async def test_request_phase_executes_in_priority_order(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=100)
        mw_b = _CountingMiddleware(instance_id="B", priority=50)
        chain.add_all([mw_a, mw_b])

        await chain.run_request(_make_req_ctx())

        assert mw_b.calls == ["req:B"]  # B runs first (lower priority)
        assert mw_a.calls == ["req:A"]  # A runs second

    @pytest.mark.asyncio
    async def test_response_phase_executes_in_priority_order(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=100)
        mw_b = _CountingMiddleware(instance_id="B", priority=50)
        chain.add_all([mw_a, mw_b])

        await chain.run_response(_make_resp_ctx())

        assert mw_b.calls == ["resp:B"]
        assert mw_a.calls == ["resp:A"]

    @pytest.mark.asyncio
    async def test_empty_chain_passes_through(self):
        chain = MiddlewareChain()

        req = await chain.run_request(_make_req_ctx())
        assert req.trace_id == "t1"

        resp = await chain.run_response(_make_resp_ctx())
        assert resp.response.content == "ok"


# ------------------------------------------------------------------ Exception propagation — request phase


class TestRequestExceptionPropagation:
    @pytest.mark.asyncio
    async def test_block_exception_stops_chain(self):
        """BlockException in middleware N stops execution of N+1 onward."""
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_request=BlockException)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        with pytest.raises(BlockException):
            await chain.run_request(_make_req_ctx())

        assert mw_a.calls == ["req:A"]  # A called
        assert mw_b.calls == []  # B NOT called — chain stopped

    @pytest.mark.asyncio
    async def test_rate_limit_exception_stops_chain(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_request=RateLimitException)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        with pytest.raises(RateLimitException):
            await chain.run_request(_make_req_ctx())

        assert mw_a.calls == ["req:A"]
        assert mw_b.calls == []

    @pytest.mark.asyncio
    async def test_generic_exception_does_not_stop_chain(self):
        """A generic Exception in middleware N is logged, BUT middleware N+1 still runs."""
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_request=ValueError)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        ctx = await chain.run_request(_make_req_ctx())

        assert mw_a.calls == ["req:A"]  # A called (crashed)
        assert mw_b.calls == ["req:B"]  # B still called
        assert ctx.trace_id == "t1"  # initial ctx passed through


# ------------------------------------------------------------------ Exception propagation — response phase


class TestResponseExceptionPropagation:
    @pytest.mark.asyncio
    async def test_block_in_response_does_not_stop_chain(self):
        """Response has been sent — BlockException is logged, next middleware runs."""
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_response=BlockException)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        ctx = await chain.run_response(_make_resp_ctx())

        assert mw_a.calls == ["resp:A"]
        assert mw_b.calls == ["resp:B"]  # still runs after A's BlockException
        assert ctx.response.content == "ok"

    @pytest.mark.asyncio
    async def test_generic_exception_in_response_does_not_stop_chain(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_response=ValueError)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        ctx = await chain.run_response(_make_resp_ctx())

        assert mw_a.calls == ["resp:A"]
        assert mw_b.calls == ["resp:B"]


# ------------------------------------------------------------------ Stream chunk phase


class TestStreamChunkPropagation:
    @pytest.mark.asyncio
    async def test_chunk_passes_through_chain(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)
        chain.add_all([mw_a, mw_b])

        chunk = StreamChunk(delta_content="hello")
        result = await chain.run_stream_chunk(chunk, _make_stream_ctx())

        assert result is not None
        assert result.delta_content == "hello"
        assert "chk:A" in mw_a.calls
        assert "chk:B" in mw_b.calls

    @pytest.mark.asyncio
    async def test_drop_chunk_returns_none(self):
        """Middleware returning None drops the chunk."""
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, drop_stream_chunk=True)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        chunk = StreamChunk(delta_content="bad")
        result = await chain.run_stream_chunk(chunk, _make_stream_ctx())

        assert result is None
        assert "chk:B" not in mw_b.calls  # B never runs after drop

    @pytest.mark.asyncio
    async def test_block_exception_in_stream_drops_chunk(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_chunk=BlockException)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        chunk = StreamChunk(delta_content="blocked")
        result = await chain.run_stream_chunk(chunk, _make_stream_ctx())

        assert result is None
        assert "chk:B" not in mw_b.calls

    @pytest.mark.asyncio
    async def test_generic_exception_in_stream_passes_chunk_through(self):
        chain = MiddlewareChain()
        mw_a = _CountingMiddleware(instance_id="A", priority=10, raise_on_chunk=ValueError)
        mw_b = _CountingMiddleware(instance_id="B", priority=20)

        chain.add_all([mw_a, mw_b])

        chunk = StreamChunk(delta_content="still passes")
        result = await chain.run_stream_chunk(chunk, _make_stream_ctx())

        assert result is not None
        assert result.delta_content == "still passes"
        assert "chk:B" in mw_b.calls


# ------------------------------------------------------------------ BlockException / RateLimitException details


class TestExceptionDetails:
    def test_block_exception_carries_context(self):
        exc = BlockException(rule_id="rule-1", reason="bad content", status_code=403)
        assert exc.rule_id == "rule-1"
        assert exc.reason == "bad content"
        assert exc.status_code == 403
        assert str(exc) == "Blocked by rule-1: bad content"

    def test_rate_limit_exception_carries_retry_after(self):
        exc = RateLimitException(rule_id="rate-1", reason="too many", retry_after=2.5)
        assert exc.rule_id == "rate-1"
        assert exc.reason == "too many"
        assert exc.status_code == 429
        assert exc.retry_after == 2.5
