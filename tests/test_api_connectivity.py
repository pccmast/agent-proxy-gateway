"""Integration tests that auto-start the gateway and verify all API endpoints.

These tests use FastAPI TestClient (no real server needed) to verify:
1. All management API endpoints return correct schemas
2. Dashboard-facing endpoints are reachable
3. Port configuration is consistent across the codebase

Run with:
    uv run pytest tests/test_api_connectivity.py -v
"""

import pytest
from fastapi.testclient import TestClient

from gateway.main import create_app
from shared.constants import DEFAULT_GATEWAY_PORT, DEFAULT_GATEWAY_URL


@pytest.fixture
def client():
    """Create a TestClient with lifespan enabled (initializes DB, policy store, etc.)."""
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestHealthEndpoint:
    """Verify /health returns expected schema."""

    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "host" in data
        assert "port" in data
        assert data["port"] == DEFAULT_GATEWAY_PORT


class TestTracesAPI:
    """Verify traces endpoints return correct schemas."""

    def test_list_traces_schema(self, client):
        resp = client.get("/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data
        assert "count" in data
        assert isinstance(data["traces"], list)
        assert isinstance(data["count"], int)

    def test_traces_stats_schema(self, client):
        resp = client.get("/api/traces/stats")
        assert resp.status_code == 200
        data = resp.json()
        # Should contain aggregation fields
        assert "total_requests" in data
        assert "avg_latency_ms" in data
        assert "p50_latency_ms" in data
        assert "p95_latency_ms" in data
        assert "p99_latency_ms" in data

    def test_trace_detail_404_for_unknown(self, client):
        resp = client.get("/api/traces/nonexistent-trace-id")
        assert resp.status_code == 404


class TestGuardrailsAPI:
    """Verify guardrails endpoints return correct schemas."""

    def test_guardrails_stats_schema(self, client):
        resp = client.get("/api/guardrails/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "stats" in data
        assert "total_hits" in data
        assert isinstance(data["stats"], dict)
        assert isinstance(data["total_hits"], int)

    def test_guardrails_rules_schema(self, client):
        resp = client.get("/api/guardrails/rules")
        assert resp.status_code == 200
        data = resp.json()
        assert "rules" in data
        assert isinstance(data["rules"], list)
        for rule in data["rules"]:
            assert "id" in rule
            assert "action" in rule
            assert "enabled" in rule


class TestBudgetAPI:
    """Verify budget endpoint returns correct schema."""

    def test_budget_status_schema(self, client):
        resp = client.get("/api/budget/status?agent_id=default")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_id" in data
        assert "hourly_used" in data
        assert "hourly_limit" in data
        assert "hourly_ratio" in data
        assert "daily_used" in data
        assert "daily_limit" in data
        assert "daily_ratio" in data
        assert "budget_ok" in data


class TestEvalAPI:
    """Verify eval endpoint returns correct schema."""

    def test_eval_metrics_schema(self, client):
        resp = client.get("/api/eval/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        assert isinstance(data["metrics"], list)
        assert len(data["metrics"]) > 0


class TestPortConsistency:
    """Verify port configuration is consistent across the codebase."""

    def test_default_port_not_common(self):
        """Default port should not be a commonly used port like 8080."""
        common_ports = {80, 443, 8080, 3000, 5000, 8000, 9000}
        assert DEFAULT_GATEWAY_PORT not in common_ports, (
            f"DEFAULT_GATEWAY_PORT ({DEFAULT_GATEWAY_PORT}) is a commonly used port. "
            "Choose a less common port to avoid conflicts."
        )

    def test_default_port_is_high(self):
        """Default port should be in the ephemeral/high range to avoid conflicts."""
        assert DEFAULT_GATEWAY_PORT >= 1024, (
            f"DEFAULT_GATEWAY_PORT ({DEFAULT_GATEWAY_PORT}) should be >= 1024"
        )

    def test_yaml_config_matches_default(self):
        """config/default.yaml port should match the code default."""
        import yaml
        from pathlib import Path

        config_path = Path(__file__).parent.parent / "config" / "default.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)

        yaml_port = config.get("proxy", {}).get("port")
        assert yaml_port == DEFAULT_GATEWAY_PORT, (
            f"config/default.yaml proxy.port ({yaml_port}) != "
            f"DEFAULT_GATEWAY_PORT ({DEFAULT_GATEWAY_PORT}). "
            "Keep them in sync."
        )
