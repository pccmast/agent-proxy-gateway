# Agent Proxy Gateway — Docker image
# 
# Build:
#   docker build -t agent-gateway .
#
# Run:
#   docker run -p 8080:8080 -e OPENAI_API_KEY=sk-... agent-gateway
#
# Or use docker-compose up

FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install project dependencies (layer caching)
COPY pyproject.toml ./
RUN uv pip install --system -e .

# Copy source code
COPY config/ ./config/
COPY src/ ./src/
COPY dashboard/ ./dashboard/
COPY scripts/ ./scripts/

# Create data directory
RUN mkdir -p /app/data

# Environment
ENV PYTHONUNBUFFERED=1
ENV GATEWAY_HOST=0.0.0.0
ENV GATEWAY_PORT=8080
ENV GATEWAY_CONFIG_DIR=/app/config

EXPOSE 8080

# Default command — start gateway
CMD ["python", "-m", "gateway.main"]
