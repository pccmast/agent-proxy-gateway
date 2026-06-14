#!/usr/bin/env bash
# Docker smoke-test script — validates the agent-gateway Docker deployment.
#
# Usage:
#   bash scripts/docker_test.sh          # full test
#   bash scripts/docker_test.sh build    # build only
#   bash scripts/docker_test.sh clean    # stop + remove containers + images
#
# Prerequisites:
#   - docker + docker-compose
#   - OPENAI_API_KEY or DEEPSEEK_API_KEY in .env (needed for proxy test)
#
# Tests:
#   1. docker-compose build (gateway + dashboard)
#   2. docker-compose up -d
#   3. Health check (wait for healthy)
#   4. GET /health
#   5. GET /metrics
#   6. GET /api/traces
#   7. GET /api/guardrails/rules
#   8. POST /v1/chat/completions (proxy)
#   9. GET /api/traces/stats
#   10. Dashboard reachability
#   11. docker-compose down

set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:18080}"
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8501}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-90}"
passed=0
failed=0

# Colors
green()  { printf "\033[32m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }

# -------------------------------------------------------------- helpers

pass() { green "  PASS: $1"; passed=$((passed + 1)); }
fail() { red   "  FAIL: $1"; failed=$((failed + 1)); }
skip() { yellow "  SKIP: $1"; }

# Make an HTTP request and check the status code
check_get() {
    local description="$1" url="$2" expected="${3:-200}"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null || echo "000")
    if [ "$code" = "$expected" ]; then
        pass "$description"
    else
        fail "$description (expected $expected, got $code)"
    fi
}

check_post() {
    local description="$1" url="$2" data="$3" expected="${4:-200}"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
        -H "Content-Type: application/json" -H "Authorization: Bearer any-key" \
        -d "$data" 2>/dev/null || echo "000")
    if [ "$code" = "$expected" ]; then
        pass "$description"
    else
        fail "$description (expected $expected, got $code)"
    fi
}

# -------------------------------------------------------------- main

case "${1:-test}" in
    clean)
        echo "=== Cleaning up ==="
        docker-compose down -v --remove-orphans 2>/dev/null || true
        docker rmi agent-gateway agent-gateway-dashboard 2>/dev/null || true
        green "Cleanup done"
        exit 0
        ;;

    build)
        echo "=== 1. Building images ==="
        if docker-compose build --no-cache; then
            pass "docker-compose build"
            green "Build done — run 'bash scripts/docker_test.sh test' to continue"
        else
            fail "docker-compose build"
        fi
        exit 0
        ;;
esac

echo "================================================================"
echo " Docker Smoke Test — Agent Gateway"
echo " Gateway: $GATEWAY_URL"
echo " Dashboard: $DASHBOARD_URL"
echo "================================================================"
echo ""

# ---- 1. Build ----
echo "--- 1. Build ---"
if ! docker-compose build 2>&1 | tail -5; then
    fail "docker-compose build"
    exit 1
fi
pass "docker-compose build"

# ---- 2. Start ----
echo "--- 2. Start ---"
docker-compose up -d 2>&1
pass "docker-compose up"

# ---- 3. Wait for healthy ----
echo "--- 3. Health check (waiting up to ${TIMEOUT_SECONDS}s) ---"
elapsed=0
healthy=false
while [ $elapsed -lt $TIMEOUT_SECONDS ]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' agent-gateway 2>/dev/null || echo "none")
    if [ "$status" = "healthy" ]; then
        healthy=true
        break
    fi
    sleep 3
    elapsed=$((elapsed + 3))
    echo "  ... waiting ($elapsed s, status=$status)"
done

if $healthy; then
    pass "gateway healthy after ${elapsed}s"
else
    fail "gateway not healthy within ${TIMEOUT_SECONDS}s"
    echo "  Last 20 logs:"
    docker logs --tail 20 agent-gateway 2>/dev/null || true
    exit 1
fi

# ---- 4. /health ----
echo "--- 4. /health ---"
check_get "GET /health" "$GATEWAY_URL/health"

# ---- 5. /metrics ----
echo "--- 5. /metrics ---"
check_get "GET /metrics (Prometheus)" "$GATEWAY_URL/metrics"

# ---- 6. /api/traces ----
echo "--- 6. /api/traces ---"
check_get "GET /api/traces" "$GATEWAY_URL/api/traces"

# ---- 7. /api/guardrails/rules ----
echo "--- 7. /api/guardrails/rules ---"
check_get "GET /api/guardrails/rules" "$GATEWAY_URL/api/guardrails/rules"

# ---- 8. Proxy request ----
echo "--- 8. POST /v1/chat/completions (proxy) ---"
if [ -n "${OPENAI_API_KEY:-}" ] || [ -n "${DEEPSEEK_API_KEY:-}" ]; then
    response=$(curl -s -w "\n%{http_code}" -X POST "$GATEWAY_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer any-key" \
        -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Say hi in one word"}],"max_tokens":5}' \
        2>/dev/null || echo "000")
    code=$(echo "$response" | tail -1)
    body=$(echo "$response" | head -n -1)
    if [ "$code" = "200" ]; then
        pass "POST proxy (status=$code)"
    elif [ "$code" = "502" ] || [ "$code" = "504" ]; then
        skip "POST proxy (upstream unreachable from Docker — expected in local env)"
    else
        fail "POST proxy (status=$code)"
    fi
else
    skip "POST proxy (no API key in .env)"
fi

# ---- 9. /api/traces/stats ----
echo "--- 9. /api/traces/stats ---"
check_get "GET /api/traces/stats" "$GATEWAY_URL/api/traces/stats"

# ---- 10. Dashboard ----
echo "--- 10. Dashboard ---"
check_get "GET dashboard" "$DASHBOARD_URL"

# ---- Summary ----
echo ""
echo "================================================================"
total=$((passed + failed))
green "Results: $passed passed, $failed failed (total $total checks)"
echo "================================================================"

# Keep running or stop
if [ "${KEEP_RUNNING:-0}" = "1" ]; then
    yellow "Containers kept running. Use 'bash scripts/docker_test.sh clean' to stop."
else
    echo "Cleaning up..."
    docker-compose down -v --remove-orphans 2>/dev/null || true
    green "Containers removed"
fi

[ $failed -eq 0 ] || exit 1
