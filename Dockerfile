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

# Install system deps (use Aliyun Debian mirror for speed in China)
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure pip & uv to use Aliyun PyPI mirror (faster in China)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# Install uv via pip
RUN pip install --no-cache-dir uv

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
