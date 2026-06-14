"""Prometheus metrics for the agent gateway.

Exposes a ``/metrics`` endpoint with counters and gauges for key gateway
signals: request volume, latency distribution, guardrail hits, circuit-breaker
state, and trace-store write failures.

Uses a dedicated ``CollectorRegistry`` to avoid import-ordering issues
between prometheus-client's default registry and our lazy-imported module.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

# Dedicated registry — guarantees Counter/Gauge/Histogram constructors
# register into the same registry that generate_latest() reads.
_REGISTRY = CollectorRegistry(auto_describe=True)

# ------------------------------------------------------------------ Request counters

gateway_requests_total = Counter(
    "gateway_requests_total",
    "Total requests processed by the gateway",
    ["status"],  # ok | blocked | rate_limited | timeout | error | abandoned
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Latency histogram

gateway_latency_seconds = Histogram(
    "gateway_latency_seconds",
    "End-to-end request latency (seconds)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Guardrail metrics

guardrail_hits_total = Counter(
    "guardrail_hits_total",
    "Total guardrail rule hits",
    ["rule_id", "action"],
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Circuit breaker

circuit_breaker_state = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open, 2=half_open)",
    ["provider"],
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Trace health

trace_write_failures_total = Gauge(
    "gateway_trace_write_failures_total",
    "Cumulative trace store write failures since start",
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Session store

session_count = Gauge(
    "gateway_sessions_active",
    "Number of active guardrail sessions",
    registry=_REGISTRY,
)

# ------------------------------------------------------------------ Helpers


def record_request(status: str, latency_s: float = 0.0) -> None:
    """Increment the request counter and observe latency."""
    gateway_requests_total.labels(status=status).inc()
    if latency_s > 0:
        gateway_latency_seconds.observe(latency_s)


def metrics_response() -> str:
    """Return Prometheus text format for the /metrics endpoint."""
    return generate_latest(_REGISTRY).decode("utf-8")
