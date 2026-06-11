# Agent Proxy Gateway

**English** | [📖 简体中文](README_zh.md)

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
│  │ Priority 10: GuardrailsEngine                    │   │
│  │   ├── PII Detection (redact email/phone/ID)      │   │
│  │   ├── Injection Detection (block attacks)        │   │
│  │   └── Content Safety (block harmful content)     │   │
│  │                                                   │   │
│  │ Priority 15: SlidingWindowRateLimiter            │   │
│  │   └── RPM / TPM sliding-window throttling        │   │
│  │                                                   │   │
│  │ Priority 90: EvalPipeline                        │   │
│  │   ├── ResponseLength / Repetition / Latency      │   │
│  │   ├── ToolCall (heuristic evals, sync)           │   │
│  │   └── LLM-as-Judge (relevance/safety/coherence)  │   │
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
└────────────────────┬────────────────────────────────────┘
                     ▼
           LLM / Tool Backend API
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- OpenAI API key (and optionally Anthropic)

### Install & Run

```bash
# Clone
git clone <repo-url>
cd agent-gateway

# Install dependencies
uv pip install -e ".[dev]"

# Set API key
export OPENAI_API_KEY=sk-your-key

# Start gateway
uv run gateway
# → http://localhost:18080

# (Optional) Start dashboard
uv run streamlit run dashboard/app.py
# → http://localhost:8501
```

### Test It

```bash
# Health check
curl http://localhost:18080/health

# Proxy a chat completion
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Hello!"}],"max_tokens":10}'

# Check traces
curl http://localhost:18080/api/traces

# Run demo
uv run python scripts/demo.py
```

### Generate Test Data

```bash
uv run python scripts/seed_data.py --count 50
```

### Docker

```bash
docker-compose up -d
# Gateway: http://localhost:18080
# Dashboard: http://localhost:8501
```

## Features

| Feature | Status | Description |
|---------|--------|-------------|
| **Transparent Proxy** | ✅ | Agent only changes `base_url` — no code changes |
| **Multi-Provider** | ✅ | OpenAI + Anthropic adapters, extensible registry |
| **Streaming (SSE)** | ✅ | Chunk-by-chunk forwarding with real-time guardrails |
| **Trace Engine** | ✅ | Full request lifecycle tracing with span trees |
| **PII Guardrail** | ✅ | Email, phone, ID, bank card detection & redaction |
| **Injection Guardrail** | ✅ | Prompt injection attack detection & blocking |
| **Content Safety** | ✅ | Violence, self-harm, illegal content filtering |
| **Rate Limiting** | ✅ | RPM/TPM sliding-window per agent and model |
| **Token Budget** | ✅ | Hourly/daily limits with 80% warning threshold |
| **Circuit Breaker** | ✅ | CLOSED → OPEN → HALF_OPEN state machine |
| **Eval Pipeline** | ✅ | 4 heuristic evals + optional LLM-as-Judge |
| **Dashboard** | ✅ | Streamlit UI: Traces, Guardrails, Budget, Eval |
| **Docker** | ✅ | Dockerfile + docker-compose.yml |
| **Policy Hot-Reload** | ✅ | YAML config changes picked up automatically |

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
│       ├── api/              # Management API endpoints
│       ├── budget/           # Rate limiter, token counter, circuit breaker
│       ├── eval/             # Heuristic evals, LLM judge, eval pipeline
│       ├── guardrails/       # PII, injection, content safety rules
│       ├── policy/           # YAML config loading + Pydantic validation
│       ├── proxy/            # Proxy engine, SSE interceptor, middleware chain
│       ├── trace/            # Trace engine, SQLite store, span tree
│       └── main.py           # FastAPI entry point
├── dashboard/
│   ├── app.py                # Streamlit dashboard entry
│   └── pages/                # Overview, Traces, Guardrails, Budget, Eval
├── scripts/
│   ├── demo.py               # End-to-end demo
│   └── seed_data.py          # Test data generator
├── tests/                    # 86 unit + integration tests
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

## API Endpoints

### Proxy (Agent-facing)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/{path}` | Transparent proxy to upstream |

### Management

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
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

```yaml
guardrails:
  enabled: true
  rules:
    - id: "pii-detection"
      type: "pii"
      action: "redact"     # block | redact | log
      confidence_threshold: 0.7
      enabled: true

    - id: "injection-detection"
      type: "injection"
      action: "block"
      patterns: ["ignore previous instructions", "system override", ...]
      enabled: true

    - id: "content-safety"
      type: "content"
      action: "block"
      enabled: true
```

### Budget (`config/default.yaml`)

```yaml
budget:
  defaults:
    max_tokens_per_day: 1000000
    max_tokens_per_hour: 100000
    warning_threshold: 0.8

rate_limit:
  defaults:
    rpm: 60
    tpm: 100000

circuit_breaker:
  failure_threshold: 5
  recovery_timeout: 30
```

## Design Decisions

1. **Middleware Chain (not Filter Chain)**: Bi-directional — middleware can intercept both requests *and* responses, which is essential for guardrails that check both input and output.

2. **SQLite for MVP**: Zero external dependencies, works out of the box. Migrate to PostgreSQL by changing one connection string.

3. **Block vs Redact vs Log**:
   - **Block**: Definitely harmful (injection, severe violations)
   - **Redact**: Sensitive but not fatal (PII) — strip PII, pass the rest
   - **Log**: Suspicious but uncertain — record for human review

4. **Heuristic + LLM Eval**: Heuristic evals run on every request (zero cost, deterministic). LLM-as-Judge runs asynchronously on sampled requests (higher quality, not blocking).

5. **TypedDict for Protocol Adapters**: Each adapter uses TypedDict for its provider-specific JSON schema, providing IDE autocomplete and catching field typos at dev time.

## Testing

```bash
# All tests (excludes integration tests that need live API keys)
uv run pytest tests/ -v

# With coverage
uv run pytest tests/ --cov=src/gateway --cov-report=html

# Integration tests (requires OPENAI_API_KEY)
uv run pytest tests/ -v -k integration
```

## License

MIT
