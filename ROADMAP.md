# Project Roadmap — deferred & future items

Items marked with a priority level and reasoning so future contributors
(and interviewers) understand what was consciously deferred vs. overlooked.

---

## Deferred (plan later — currently unnecessary overhead)

### Multi-tenant isolation
**Why deferred**: The gateway currently serves a single Agent product / team.
All guardrail rules, rate-limit quotas, and budget counters are shared globally.
This is fine for intra-team deployments where one team owns all agents.

**When to revisit**: When two or more independent teams share the same gateway
instance and need per-tenant:
- Separate guardrail thresholds (team-A's PII sensitivity != team-B's)
- Independent RPM / token budgets (team-A's traffic spike must not rate-limit team-B)

**Estimated effort**: ~200 LOC — add `tenant_id` dimension to RateLimiter,
TokenCounter, GuardrailsEngine scope, and SessionStore.

### Redis SessionStore
**Why deferred**: SQLite persistence (2025-06-14) already survives gateway
restarts. Redis is only necessary when:
- The gateway runs in multiple replicas (horizontal scaling)
- Session state must be shared across processes

Single-instance deployments are the common case for gateway-size services;
Redis adds an operational dependency with no benefit in that scenario.

**Estimated effort**: ~80 LOC — implement the same `get_or_create / get / reset`
interface backed by Redis, swap in `main.py`.

---

## v2 (architectural changes — plan separately)

### OpenTelemetry integration
**Current state**: Self-built trace system (UUID trace_id + span_id + SQLite + SpanTree).
**Why v2**: The self-built system covers gateway-internal observability needs
well for single-service deployments. OTel brings value when:
- Traces must be exported to Jaeger / Datadog / Grafana Tempo
- Distributed tracing across Agent SDK → Gateway → LLM Backend is required

**First step** (low-cost): Parse W3C `traceparent` header (~30 LOC) so
external trace_ids flow through the gateway. Full OTel SDK adoption
(collector, exporter, auto-instrumentation) is ~500 LOC and should be
planned as a v2 milestone.

### Model routing / scheduler
**Current state**: Gateway is a transparent proxy — `base_url` from YAML config
determines the LLM backend. No dynamic routing, no fallback chains, no A/B.

**Why v2**: Model routing is a separate concern from observability + safety.
A P5 middleware (ModelRouter) can introduce routing decisions without
modifying the gateway core. If the scheduler grows heavy (stateful, needs
independent scaling), it should be deployed as a sidecar service.

### gRPC streaming support
**Current state**: HTTP/1.1 SSE only (OpenAI / Anthropic streaming protocol).
**Why v2**: Requires a new ProtocolAdapter, new SSE-equivalent interceptor,
and is only needed when upstream LLM services (Google Vertex AI, Triton)
use gRPC. Not a blocker for OpenAI/Anthropic-centric deployments.

---

## Completed (2025-06-13 — 2025-06-14)

- RateLimitException separation from BlockException
- CircuitBreaker wired into request forwarding path
- Outer exception handler + SSE finally block (no orphan traces)
- Abandoned-span background cleanup task
- 6 trace statuses: ok / blocked / rate_limited / timeout / error / abandoned
- Blocked-chunk placeholder replacement (not silent drop)
- LLM-Judge removed from default config (kept as experimental code)
- 6 guardrail rule bugs fixed (PII name regex, ToolCallLoop, session sharing, etc.)
- Credential leak detection rule (auto-discovered)
- P3 RequestTimeoutGuard middleware
- 3 adapter/trace bugs fixed (Anthropic double-/v1, case-sensitive REDACT, asyncio.run)
- SQLiteSessionStore (survives gateway restarts)
- Prometheus /metrics endpoint + instrumentation
- Config validation CLI (`uv run validate-config`)
