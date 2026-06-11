"""Integration tests for the Agent Gateway.

Tests the full request lifecycle through the gateway.
These tests use the FastAPI TestClient to simulate real requests.
"""

import pytest
import os

from shared.constants import DEFAULT_GATEWAY_URL, DEFAULT_GATEWAY_PORT

# Ensure OPENAI_API_KEY is available for integration tests
# Skip if no API key configured
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("OPENAI_API_KEY"),
        reason="OPENAI_API_KEY not set — set it to run integration tests",
    ),
]


@pytest.fixture
def gateway_url():
    return os.environ.get("GATEWAY_URL", DEFAULT_GATEWAY_URL)


class TestGatewayIntegration:
    """End-to-end integration tests against the running gateway.

    Requires a running gateway instance on localhost:{DEFAULT_GATEWAY_PORT}
    and a valid OPENAI_API_KEY.
    """

    def test_health_check(self, gateway_url):
        """Health endpoint should return ok."""
        import httpx
        resp = httpx.get(f"{gateway_url}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_non_streaming_chat_completion(self, gateway_url):
        """Gateway should transparently proxy a non-streaming chat completion."""
        import httpx
        resp = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": "Say hello in exactly one word."}
                ],
                "stream": False,
                "max_tokens": 10,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer any-key",
            },
            timeout=30,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert "message" in data["choices"][0]
        assert "content" in data["choices"][0]["message"]
        assert "usage" in data

    def test_streaming_chat_completion(self, gateway_url):
        """Gateway should transparently proxy a streaming chat completion."""
        import httpx
        with httpx.stream(
            "POST",
            f"{gateway_url}/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "user", "content": "Count from 1 to 3."}
                ],
                "stream": True,
                "max_tokens": 20,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer any-key",
            },
            timeout=30,
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")

            chunks = []
            done_seen = False
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        done_seen = True
                        break
                    import json
                    chunks.append(json.loads(data_str))

            assert len(chunks) > 0
            # Should see content-rich chunks
            has_content = any(
                c.get("choices", [{}])[0].get("delta", {}).get("content")
                for c in chunks
            )
            assert has_content or done_seen

    def test_trace_recording(self, gateway_url):
        """Gateway should record traces for proxied requests."""
        import httpx
        import time

        # Send a request first
        resp = httpx.post(
            f"{gateway_url}/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "max_tokens": 5,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer any-key",
            },
            timeout=30,
        )
        assert resp.status_code == 200

        # Allow async trace writing
        time.sleep(0.5)

        # Check traces API
        traces_resp = httpx.get(f"{gateway_url}/api/traces", timeout=5)
        assert traces_resp.status_code == 200
        data = traces_resp.json()
        assert "traces" in data
        assert len(data["traces"]) > 0


class TestGatewayUnit:
    """Unit-level tests that don't require actual API keys."""

    def test_create_app_returns_fastapi(self):
        """create_app() should return a FastAPI instance."""
        from gateway.main import create_app
        app = create_app()
        assert app is not None
        assert app.title == "Agent Proxy Gateway"

    def test_health_endpoint(self):
        """Health endpoint should work without external dependencies."""
        from fastapi.testclient import TestClient
        from gateway.main import create_app

        app = create_app()
        client = TestClient(app)

        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_adapter_registry(self):
        """Should create adapter registry with OpenAI adapter."""
        from gateway.adapter.normalizer import create_registry

        registry = create_registry()
        providers = registry.list_providers()

        assert "openai" in providers
        assert registry.get("openai") is not None

    def test_middleware_chain_ordering(self):
        """Middleware chain should execute by priority."""
        from gateway.proxy.middleware import Middleware, MiddlewareChain
        from shared.models import RequestContext, NormalizedRequest

        order = []

        class M1(Middleware):
            priority = 10
            async def on_request(self, ctx):
                order.append("m1_req")
                return ctx
            async def on_response(self, ctx):
                order.append("m1_resp")
                return ctx

        class M2(Middleware):
            priority = 20
            async def on_request(self, ctx):
                order.append("m2_req")
                return ctx
            async def on_response(self, ctx):
                order.append("m2_resp")
                return ctx

        chain = MiddlewareChain()
        chain.add_all([M2(), M1()])  # Add out of order

        import asyncio

        async def run():
            ctx = RequestContext(
                trace_id="test",
                span_id="test",
                request=NormalizedRequest(provider="test", model="test", messages=[]),
            )
            await chain.run_request(ctx)

        asyncio.run(run())

        # Lower priority runs first
        assert order == ["m1_req", "m2_req"]

    def test_block_exception(self):
        """BlockException should carry rule_id and reason."""
        from gateway.proxy.middleware import BlockException

        exc = BlockException(rule_id="test-rule", reason="testing", status_code=403)
        assert exc.rule_id == "test-rule"
        assert exc.reason == "testing"
        assert exc.status_code == 403
        assert "test-rule" in str(exc)
