#!/usr/bin/env bash
# Docker smoke-test script — validates the agent-gateway Docker deployment.
#
# Usage:
#   bash scripts/docker_test.sh          # full test (build + start + test + stop)
#   bash scripts/docker_test.sh build    # build only, keep images
#   bash scripts/docker_test.sh clean    # stop + remove containers + images
#
# Env vars:
#   GATEWAY_URL     default http://localhost:18080
#   DASHBOARD_URL   default http://localhost:8502
#   TIMEOUT_SECONDS default 90
#   KEEP_RUNNING=1  don't tear down after test (manual inspection)
#   VERBOSE=1       print response bodies for every check
#
# Prerequisites: docker + docker-compose

set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:18080}"
DASHBOARD_URL="${DASHBOARD_URL:-http://localhost:8502}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-90}"
TMPDIR="${TMPDIR:-/tmp}"
LOG_FILE="$TMPDIR/gateway_docker_test_$$.log"
PASSED=0; FAILED=0; SKIPPED=0
START_TS=$(date +%s)

# ── helpers ──────────────────────────────────────────────────────────

green()  { printf "\033[32m%s\033[0m\n" "$1"; }
red()    { printf "\033[31m%s\033[0m\n" "$1"; }
yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
dim()    { printf "\033[2m%s\033[0m\n" "$1"; }

pass() { green "  PASS  $1"; PASSED=$((PASSED + 1)); }
fail() {
    red   "  FAIL  $1"
    FAILED=$((FAILED + 1))
    [ -s "$LOG_FILE" ] && { dim "  ── response ──"; dim "$(head -c 500 "$LOG_FILE")"; > "$LOG_FILE"; }
}
skip() { yellow "  SKIP  $1"; SKIPPED=$((SKIPPED + 1)); }
info() { printf "  %s\n" "$1"; }

elapsed() { echo "$(( $(date +%s) - START_TS ))s"; }

# HTTP helpers — save response body to LOG_FILE for debugging
do_get() {
    local url="$1" expected="${2:-200}"
    local code
    code=$(curl -s -w "%{http_code}" -o "$LOG_FILE" --max-time 10 "$url" 2>/dev/null || echo "000")
    [ "${VERBOSE:-0}" = "1" ] && [ -s "$LOG_FILE" ] && dim "$(head -c 200 "$LOG_FILE")"
    echo "$code"
}

do_post() {
    local url="$1" data="$2" expected="${3:-200}"
    local code
    code=$(curl -s -w "%{http_code}" -o "$LOG_FILE" --max-time 30 \
        -X POST "$url" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer any-key" \
        -d "$data" 2>/dev/null || echo "000")
    [ "${VERBOSE:-0}" = "1" ] && [ -s "$LOG_FILE" ] && dim "$(head -c 200 "$LOG_FILE")"
    echo "$code"
}

check_get() {
    local desc="$1" url="$2" expected="${3:-200}"
    local code
    info "$desc"
    code=$(do_get "$url" "$expected")
    if [ "$code" = "$expected" ]; then
        pass "$desc ($code)"
    else
        fail "$desc — expected $expected, got $code"
    fi
}

check_post() {
    local desc="$1" url="$2" data="$3" expected="${4:-200}"
    local code
    info "$desc"
    code=$(do_post "$url" "$data" "$expected")
    if [ "$code" = "$expected" ]; then
        pass "$desc ($code)"
    else
        fail "$desc — expected $expected, got $code"
    fi
}

check_content() {
    local desc="$1" url="$2" pattern="$3"
    local body code
    info "$desc"
    code=$(curl -s -w "%{http_code}" -o "$LOG_FILE" --max-time 10 "$url" 2>/dev/null || echo "000")
    body=$(cat "$LOG_FILE" 2>/dev/null || echo "")
    if [ "$code" != "200" ]; then
        fail "$desc — status $code"
        return
    fi
    if echo "$body" | grep -q "$pattern"; then
        pass "$desc (found '$pattern')"
    else
        fail "$desc — missing '$pattern'"
        dim "  body preview: $(echo "$body" | head -c 120)"
    fi
}

# ── main ─────────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo " Docker Smoke Test — Agent Gateway"
echo " Gateway  : $GATEWAY_URL"
echo " Dashboard: $DASHBOARD_URL"
echo " Log file : $LOG_FILE"
echo "================================================================"
echo ""

case "${1:-test}" in
clean)
    echo "=== Cleanup ==="
    docker-compose down -v --remove-orphans 2>/dev/null || true
    docker rmi agent-gateway agent-gateway-dashboard 2>/dev/null || true
    green "Cleanup done"
    exit 0
    ;;
build)
    echo "=== Build only ==="
    docker-compose build --no-cache
    green "Build done ($(elapsed)). Run 'bash scripts/docker_test.sh test' to continue."
    exit 0
    ;;
esac

# ══════════════════════════════════════════════════════════════════
# Step 0 — pre-flight: ensure no stale containers
# ══════════════════════════════════════════════════════════════════
echo "--- Pre-flight ---"
running=$(docker ps -q --filter "name=agent-gateway")
if [ -n "$running" ]; then
    yellow "  Stale containers detected. Cleaning up first..."
    docker-compose down -v --remove-orphans 2>/dev/null || true
    sleep 2
fi
pass "pre-flight clean"

# ══════════════════════════════════════════════════════════════════
# Step 1 — Build
# ══════════════════════════════════════════════════════════════════
echo "--- 1. Build ($(elapsed)) ---"
if docker-compose build 2>&1 | tee "$TMPDIR/gateway_build.log" | tail -3; then
    pass "docker-compose build"
else
    fail "docker-compose build"
    dim "  Full build log: $TMPDIR/gateway_build.log"
    dim "  $(tail -8 "$TMPDIR/gateway_build.log")"
    exit 1
fi

# ══════════════════════════════════════════════════════════════════
# Step 2 — Start + health wait
# ══════════════════════════════════════════════════════════════════
echo "--- 2. Start ($(elapsed)) ---"
if ! docker-compose up -d 2>"$TMPDIR/gateway_up.log"; then
    fail "docker-compose up"
    dim "$(cat "$TMPDIR/gateway_up.log")"
    exit 1
fi
pass "docker-compose up"
info "Waiting for healthy (timeout=${TIMEOUT_SECONDS}s)..."

elapsed=0
healthy=false
while [ $elapsed -lt $TIMEOUT_SECONDS ]; do
    status=$(docker inspect --format='{{.State.Health.Status}}' agent-gateway 2>/dev/null || echo "none")

    # Don't query health endpoint during start_period (first 10s)
    if [ "$status" = "healthy" ]; then
        healthy=true; break
    fi

    # Exponential-ish backoff: 2, 2, 3, 3, 5, 5, 5, ...
    if [ $elapsed -lt 10 ]; then delay=2
    elif [ $elapsed -lt 30 ]; then delay=3
    else delay=5
    fi
    sleep $delay
    elapsed=$((elapsed + delay))
    info "  ... ${elapsed}s (status=$status)"
done

if $healthy; then
    pass "gateway healthy after ${elapsed}s"
else
    fail "gateway not healthy within ${TIMEOUT_SECONDS}s"
    echo ""
    echo "  ── container status ──"
    docker inspect --format='Health={{.State.Health.Status}} ExitCode={{.State.ExitCode}}' agent-gateway 2>/dev/null || true
    echo "  ── last 30 log lines ──"
    docker logs --tail 30 agent-gateway 2>/dev/null || true
    exit 1
fi

# ══════════════════════════════════════════════════════════════════
# Steps 3-10 — API checks
# ══════════════════════════════════════════════════════════════════
echo "--- 3. /health ($(elapsed)) ---"
check_get "GET /health" "$GATEWAY_URL/health"

echo "--- 4. /metrics ($(elapsed)) ---"
check_content "GET /metrics" "$GATEWAY_URL/metrics" "gateway_requests_total"

echo "--- 5. /api/traces ($(elapsed)) ---"
check_content "GET /api/traces" "$GATEWAY_URL/api/traces" '"traces"'

echo "--- 6. /api/guardrails/rules ($(elapsed)) ---"
check_content "GET /api/guardrails/rules" "$GATEWAY_URL/api/guardrails/rules" '"rules"'

echo "--- 7. /api/traces/stats ($(elapsed)) ---"
check_get "GET /api/traces/stats" "$GATEWAY_URL/api/traces/stats"

# ══════════════════════════════════════════════════════════════════
# Step 8 — Proxy forwarding (always run — 502 still proves gateway works)
# ══════════════════════════════════════════════════════════════════
echo "--- 8. POST /v1/chat/completions ($(elapsed)) ---"
code=$(do_post "$GATEWAY_URL/v1/chat/completions" \
    '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}],"max_tokens":3}')
case "$code" in
    200) pass "POST proxy — upstream reachable ($code)" ;;
    502|504) pass "POST proxy — gateway pathway OK, upstream unreachable ($code)" ;;
    503) skip "POST proxy — circuit breaker open ($code)" ;;
    *)   fail "POST proxy — unexpected status $code" ;;
esac

# ══════════════════════════════════════════════════════════════════
# Step 9 — Config validation inside container
# ══════════════════════════════════════════════════════════════════
echo "--- 9. Config validation inside container ($(elapsed)) ---"
if docker exec agent-gateway python -c "
import sys; sys.path.insert(0,'/app/src')
from gateway.policy.store import PolicyStore
s = PolicyStore(config_dir='/app/config')
s.reload()
print(f'OK: {len(s.policy.guardrails.rules)} rules, {len(s.policy.proxy.providers)} providers')
" 2>/dev/null; then
    pass "config validation (inside container)"
else
    fail "config validation (inside container)"
fi

# ══════════════════════════════════════════════════════════════════
# Step 10 — Dashboard
# ══════════════════════════════════════════════════════════════════
echo "--- 10. Dashboard ($(elapsed)) ---"
check_get "GET dashboard" "$DASHBOARD_URL"

# ══════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════
echo ""
echo "================================================================"
echo " Results  ($(elapsed))"
echo "================================================================"
green " Passed  : $PASSED"
if [ "$FAILED" -gt 0 ]; then red   " Failed  : $FAILED"; fi
if [ "$SKIPPED" -gt 0 ]; then yellow " Skipped : $SKIPPED"; fi
echo " Log     : $LOG_FILE"
echo "================================================================"

# Tear down
if [ "${KEEP_RUNNING:-0}" = "1" ]; then
    echo ""
    yellow "Containers kept running (KEEP_RUNNING=1)."
    echo "  Stop: docker-compose down"
    echo "  Curl: curl $GATEWAY_URL/health"
else
    echo ""
    echo "Stopping..."
    docker-compose down -v --remove-orphans 2>/dev/null || true
fi

[ "$FAILED" -eq 0 ] || exit 1
