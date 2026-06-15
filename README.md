# Agent Proxy Gateway

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Transparent proxy gateway between AI Agents and LLM/Tool APIs** — intercept, trace, guardrail, evaluate, and control all Agent traffic.

## Architecture

```
Agent (OpenAI / Anthropic SDK)
  │  HTTP Request
  ▼
┌─────────────────────────────────────────────────────────┐
│                   FastAPI Gateway                        │
│                                                          │
│  ┌────────────── Middleware Chain ──────────────────┐   │
│  │ Priority 3:  RequestTimeoutGuard                 │   │
│  │ Priority 10: GuardrailsEngine (11 rules)         │   │
│  │   ├── PII / Credential Leak / Injection          │   │
│  │   ├── Content Safety / System Prompt / Topic     │   │
│  │   ├── Jailbreak / Tool-Call Loop / Hallucination │   │
│  │   └── Output Format / Agency / Anomaly           │   │
│  │ Priority 15: SlidingWindowRateLimiter            │   │
│  │ Priority 90: EvalPipeline                        │   │
│  │   ├── ResponseLength / Repetition / Latency      │   │
│  │   └── ToolCall (4 heuristic evals, sync)         │   │
│  └──────────────────────────────────────────────────┘   │
│                          ▼                               │
│  ┌────────────── Protocol Adapter ──────────────────┐   │
│  │  OpenAI Adapter  /  Anthropic Adapter             │   │
│  │  normalize → forward → normalize                  │   │
│  └──────────────────┬──────────────────────────────┘   │
│                     ▼                                    │
│  ┌────────────── Trace Engine ──────────────────────┐   │
│  │  trace_id / span_id / span tree → SQLite          │   │
│  └──────────────────────────────────────────────────┘   │
│                     ▼                                    │
│  ┌────────────── Observability ─────────────────────┐   │
│  │  /metrics (Prometheus) + /health                  │   │
│  └──────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     ▼
           LLM / Tool Backend API
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- An LLM API key (OpenAI, DeepSeek, or Anthropic)

### Install & Configure

```bash
# Clone
git clone <repo-url>
cd agent-gateway

# Install dependencies
uv sync

# Copy and edit config
cp .env.example .env
# → set OPENAI_API_KEY=sk-your-key
```

The gateway routes requests to the upstream provider configured in `config/default.yaml`:

```yaml
proxy:
  providers:
    openai:
      base_url: "https://api.deepseek.com"    # ← can point to any OpenAI-compatible API
      api_key_env: "OPENAI_API_KEY"
    anthropic:
      base_url: "https://api.anthropic.com"
      api_key_env: "ANTHROPIC_API_KEY"
```

### Start

```bash
# Start gateway
uv run gateway
# → http://localhost:18080

# (Optional) Start dashboard
uv run streamlit run dashboard/app.py --server.port 8501
# → http://localhost:8501

# Validate config (dry-run — no server started)
uv run validate-config
```

### Test It

```bash
# Health check
curl http://localhost:18080/health

# Proxy a chat completion (via OpenAI adapter)
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hello!"}],"max_tokens":10}'

# Check traces
curl http://localhost:18080/api/traces

# Check Prometheus metrics
curl http://localhost:18080/metrics

# Run end-to-end demo
uv run python scripts/demo.py
```

### Docker

```bash
docker-compose up -d
# Gateway:  http://localhost:18080
# Dashboard: http://localhost:8501
```

## Features

| Feature | Status | Description |
|---------|--------|-------------|
| **Transparent Proxy** | ✅ | Agent only changes `base_url` — no code changes |
| **Multi-Provider** | ✅ | OpenAI + Anthropic adapters, extensible registry |
| **Streaming (SSE)** | ✅ | Chunk-by-chunk forwarding with real-time guardrails |
| **Trace Engine** | ✅ | Full request lifecycle tracing with span trees (6 statuses) |
| **6 Trace Statuses** | ✅ | ok / blocked / rate_limited / timeout / error / abandoned |
| **PII Guardrail** | ✅ | Email, phone, ID, bank card detection & redaction |
| **Credential Leak** | ✅ | API keys, JWT, AWS keys, connection strings in prompts |
| **Injection Guardrail** | ✅ | Prompt injection attack detection & blocking |
| **Content Safety** | ✅ | Violence, self-harm, illegal content filtering |
| **11 Guardrail Rules** | ✅ | PII, injection, content, jailbreak, system-prompt, etc. |
| **Rate Limiting** | ✅ | RPM/TPM sliding-window per model |
| **Token Budget** | ✅ | Hourly/daily limits with 80% warning threshold |
| **Circuit Breaker** | ✅ | CLOSED → OPEN → HALF_OPEN state machine |
| **Request Timeout** | ✅ | P3 full-link timeout guard (default 60s) |
| **Eval Pipeline** | ✅ | 4 heuristic evals (zero-cost, deterministic) |
| **Dashboard** | ✅ | Streamlit UI: Traces, Guardrails, Budget, Eval |
| **Docker** | ✅ | Dockerfile + docker-compose.yml |
| **Prometheus Metrics** | ✅ | /metrics endpoint: counters, histogram, gauges |
| **Config Validate CLI** | ✅ | `uv run validate-config` — pre-deploy config check |
| **SQLite Session Store** | ✅ | Jailbreak scores survive gateway restarts |
| **Policy Hot-Reload** | ✅ | YAML config changes picked up automatically |
| **Abandoned Span Cleanup** | ✅ | Background task marks orphan spans as abandoned |

## API Endpoints

### Proxy (Agent-facing)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/{path}` | Transparent proxy to upstream |

### Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/metrics` | Prometheus metrics |
| GET | `/api/traces` | Recent traces |
| GET | `/api/traces/{id}` | Trace + span tree |
| GET | `/api/traces/stats` | Trace statistics |
| GET | `/api/guardrails/stats` | Guardrail hit counts |
| GET | `/api/guardrails/rules` | Active rules |
| GET | `/api/budget/status` | Token budget usage |
| GET | `/api/eval/metrics` | Eval metric definitions |

## Configuration

Gateway behavior is controlled via YAML files in `config/`:

### Guardrails (`config/guardrails.yaml`)

11 auto-discovered rule types (drop a `.py` in `rules/` → loaded at startup):

| Rule | Type | Action | Phase |
|------|------|--------|-------|
| PII Detection | `pii` | redact | input + output |
| Credential Leak | `credential_leak` | redact | input + output |
| Injection Detection | `injection` | block | input + output |
| Content Safety | `content` | block | input + output |
| System Prompt Extraction | `system_prompt_extraction` | block | input |
| Topic Restriction | `topic_restriction` | block | input |
| Multi-Turn Jailbreak | `multi_turn_jailbreak` | block | input (behavioural) |
| System Prompt Leakage | `system_prompt_leakage` | block | output |
| Excessive Agency | `excessive_agency` | block | output |
| Tool-Call Loop | `tool_call_loop` | block | output (behavioural) |
| Hallucination Indicator | `hallucination_indicator` | log | output |

### Budget & Limits (`config/default.yaml`)

```yaml
budget:
  defaults:
    max_tokens_per_day: 1000000
    max_tokens_per_hour: 100000
    warning_threshold: 0.8

rate_limit:
  defaults:
    rpm: 60          # requests per minute
    tpm: 100000      # tokens per minute

circuit_breaker:
  failure_threshold: 5
  recovery_timeout: 30

eval:
  heuristic:
    max_response_length: 10000
    repetition_threshold: 0.3
    latency_p99_threshold_ms: 5000
```

## Project Structure

```
agent-gateway/
├── config/
│   ├── default.yaml          # Gateway + budget + rate + eval config
│   └── guardrails.yaml       # Guardrail rules configuration
├── src/
│   ├── shared/               # Pydantic models, config loader, logging
│   └── gateway/
│       ├── adapter/          # Protocol adapters (OpenAI, Anthropic)
│       ├── budget/           # Rate limiter, token counter, circuit breaker, timeout
│       ├── eval/             # Heuristic evals, eval pipeline
│       ├── guardrails/       # 11 rules, engine, session, audit, scope
│       ├── metrics.py        # Prometheus instrumentation
│       ├── policy/           # YAML config loading + Pydantic validation
│       ├── proxy/            # Proxy engine, SSE interceptor, middleware chain
│       ├── trace/            # Trace engine, SQLite store, span tree, pricing
│       └── main.py           # FastAPI entry point + CLI (gateway / validate-config)
├── dashboard/
│   ├── app.py                # Streamlit dashboard entry
│   └── pages/                # Overview, Traces, Guardrails, Budget, Eval
├── scripts/
│   ├── demo.py               # End-to-end demo (7 steps)
│   ├── seed_data.py          # Test data generator
│   ├── startup_check.py      # One-click startup verification
│   └── docker_test.sh        # Docker smoke test (10 steps)
├── tests/                    # 154 pytest functions (10 files, 3.4k lines)
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── ROADMAP.md                # Completed work + deferred items
└── README.md
```

## Design Decisions

1. **Middleware Chain (bi-directional)** — request phase has veto power (BlockException), response phase is observation-only. Chain priority order: P3(timeout) → P10(guardrails) → P15(rate-limit) → P90(eval).

2. **SQLite for MVP** — zero external dependencies, works out of the box. Two connections: aiosqlite for trace store, sqlite3 for session store.

3. **Block / Redact / Log** — three levels of severity. Block for definite harm (injection), Redact for sensitive data (PII, credentials), Log for suspicious-but-uncertain patterns.

4. **6 Trace Statuses** — ok / blocked / rate_limited / timeout / error / abandoned. Each status records exactly who wrote it and why, trace tree aggregates them with priority-aware merging.

5. **Protocol Adapters with TypedDict** — `normalize_request` + `normalize_response` convert between provider formats and the gateway's internal `NormalizedRequest`/`NormalizedResponse`. Adding a new provider is zero changes outside `adapter/`.

## Testing

```bash
# All tests (excludes integration tests that need live API keys)
uv run pytest tests/ -v

# With coverage
uv run pytest tests/ --cov=src/gateway --cov-report=html

# Integration tests (requires API key)
uv run pytest tests/ -v -k integration

# Validate config before deploy
uv run validate-config
```

## License

MIT
