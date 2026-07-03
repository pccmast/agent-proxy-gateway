# syntax=docker/dockerfile:1

# Agent Proxy Gateway — Multi-stage Dockerfile
#
# Stages:
#   - base      : shared Python base + uv
#   - gateway   : gateway-only runtime (no streamlit)
#   - dashboard : dashboard-only runtime (no fastapi/presidio/etc)
#
# Mirror / registry configuration is intentionally NOT hardcoded here.
# Each developer / CI should configure their own registry mirrors
# (e.g. via Docker Desktop → Settings → Docker Engine, or daemon.json).
#
# Build:
#   docker build --target gateway   -t agent-gateway:latest .
#   docker build --target dashboard -t agent-gateway-dashboard:latest .
#
# Or use docker-compose (selects target automatically).

# ============================================================
# Base stage — shared by both
# ============================================================
FROM python:3.11-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir uv

# Runtime environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=18080 \
    GATEWAY_CONFIG_DIR=/app/config


# ============================================================
# Gateway stage — FastAPI proxy + guardrails + trace + budget + eval
# ============================================================
FROM base AS gateway

COPY pyproject.toml uv.lock ./
# Install gateway deps (excludes streamlit + pandas which dashboard uses)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.30" \
    "httpx[http2]>=0.27" \
    "pydantic>=2.7" \
    "pydantic-settings>=2.3" \
    "aiosqlite>=0.20" \
    "presidio-analyzer>=2.2" \
    "prometheus-client>=0.20" \
    "pyyaml>=6.0" \
    "tiktoken>=0.7" \
    "structlog>=24.0"

# Copy gateway source only
COPY config/ ./config/
COPY src/ ./src/
RUN mkdir -p /app/data

EXPOSE 18080
CMD ["python", "-m", "gateway.main"]


# ============================================================
# Dashboard stage — Streamlit only (small image)
# ============================================================
FROM base AS dashboard

# Install dashboard deps only (no fastapi/presidio/tiktoken/aiosqlite)
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system \
    "streamlit>=1.35" \
    "httpx>=0.27" \
    "pandas>=2.0"

# Copy only what dashboard needs
COPY dashboard/ ./dashboard/
# Dashboard reads gateway via HTTP, no need to copy config/src

EXPOSE 8599
CMD ["streamlit", "run", "dashboard/app.py", "--server.port=8599", "--server.address=0.0.0.0"]
