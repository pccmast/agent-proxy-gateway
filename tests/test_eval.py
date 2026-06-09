"""Tests for eval pipeline and heuristic evaluators."""

import pytest

from gateway.eval.heuristic import ResponseLengthEval, RepetitionEval, LatencyEval, ToolCallEval
from gateway.eval.pipeline import EvalPipeline
from shared.models import (
    EvalResult, NormalizedResponse, ResponseContext, RequestContext, NormalizedRequest, Message, ToolCall,
)


class TestResponseLengthEval:
    @pytest.fixture
    def evaluator(self):
        return ResponseLengthEval(max_response_length=1000)

    def test_normal_length(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="Hello world")
        result = evaluator.evaluate(resp)
        assert result.score == 1.0

    def test_empty_response(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="")
        result = evaluator.evaluate(resp)
        assert result.score == 0.0

    def test_too_long(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="x" * 2000)
        result = evaluator.evaluate(resp)
        assert result.score < 0.5


class TestRepetitionEval:
    @pytest.fixture
    def evaluator(self):
        return RepetitionEval(repetition_threshold=0.3)

    def test_no_repetition(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="The quick brown fox jumps over the lazy dog")
        result = evaluator.evaluate(resp)
        assert result.score == 1.0

    def test_high_repetition(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="hello world hello world hello world hello world hello world")
        result = evaluator.evaluate(resp)
        assert result.score < 0.8

    def test_short_text(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="hi")
        result = evaluator.evaluate(resp)
        assert result.score == 1.0  # Too short to assess


class TestLatencyEval:
    @pytest.fixture
    def evaluator(self):
        return LatencyEval(p99_threshold_ms=5000.0)

    def test_normal_latency(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="ok")
        result = evaluator.evaluate(resp, latency_ms=1000.0)
        assert result.score == 1.0

    def test_high_latency(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="ok")
        result = evaluator.evaluate(resp, latency_ms=10000.0)
        assert result.score < 0.6

    def test_no_latency_data(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="ok")
        result = evaluator.evaluate(resp)
        assert result.score == 1.0


class TestToolCallEval:
    @pytest.fixture
    def evaluator(self):
        return ToolCallEval()

    def test_no_tool_calls(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content="ok")
        result = evaluator.evaluate(resp)
        assert result.score == 1.0

    def test_valid_tool_calls(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content=None, tool_calls=[
            ToolCall(id="1", name="calc", arguments={"expr": "1+1"}),
        ])
        result = evaluator.evaluate(resp)
        assert result.score == 1.0

    def test_empty_args(self, evaluator):
        resp = NormalizedResponse(provider="o", model="m", content=None, tool_calls=[
            ToolCall(id="1", name="calc", arguments={}),
        ])
        result = evaluator.evaluate(resp)
        assert result.score < 1.0


class TestEvalPipeline:
    @pytest.fixture
    def pipeline(self):
        return EvalPipeline(max_response_length=1000, repetition_threshold=0.3, latency_p99_threshold_ms=5000.0)

    @pytest.mark.asyncio
    async def test_on_response_runs_heuristics(self, pipeline):
        ctx = ResponseContext(
            trace_id="t", span_id="s",
            request=NormalizedRequest(provider="o", model="m", messages=[Message(role="user", content="hi")]),
            response=NormalizedResponse(provider="o", model="m", content="Hello, this is a normal response."),
        )
        result = await pipeline.on_response(ctx)
        assert len(result.eval_results) >= 3  # length + repetition + latency + tool_call (4 total, may skip latency)

    @pytest.mark.asyncio
    async def test_on_request_noop(self, pipeline):
        ctx = RequestContext(
            trace_id="t", span_id="s",
            request=NormalizedRequest(provider="o", model="m", messages=[Message(role="user", content="hi")]),
        )
        result = await pipeline.on_request(ctx)
        assert result == ctx  # on_request is a no-op


class TestHelpers:
    """Smoke test for class instantiation."""
    def test_response_length_eval_instantiation(self):
        e = ResponseLengthEval()
        assert e.name == "response_length"

    def test_repetition_eval_instantiation(self):
        e = RepetitionEval()
        assert e.name == "repetition"

    def test_latency_eval_instantiation(self):
        e = LatencyEval()
        assert e.name == "latency"

    def test_tool_call_eval_instantiation(self):
        e = ToolCallEval()
        assert e.name == "tool_call"

    def test_eval_pipeline_instantiation(self):
        p = EvalPipeline()
        assert p.priority == 90
