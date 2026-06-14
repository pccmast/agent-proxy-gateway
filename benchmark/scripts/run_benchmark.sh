#!/bin/bash
"""Quick start script for gateway benchmarking.

Usage:
    bash benchmark/scripts/run_benchmark.sh

This script:
  1. Checks prerequisites
  2. Starts the mock LLM server in background
  3. Waits for gateway to be ready
  4. Runs the benchmark
  5. Generates the report
  6. Cleans up background processes
"""

set -e

GATEWAY_URL="http://127.0.0.1:18080"
MOCK_PORT=18081
BENCHMARK_DIR="benchmark"
RESULTS_DIR="$BENCHMARK_DIR/results"

echo "========================================"
echo "Agent Proxy Gateway Benchmark"
echo "========================================"
echo ""

# Check prerequisites
echo "[1/6] Checking prerequisites..."

if ! command -v python &> /dev/null; then
    echo "ERROR: Python not found. Please install Python 3.11+."
    exit 1
fi

if ! python -c "import httpx" 2>/dev/null; then
    echo "ERROR: httpx not installed. Run: pip install httpx"
    exit 1
fi

# Check if gateway is running
echo "[2/6] Checking gateway status..."
if ! curl -s "$GATEWAY_URL/health" > /dev/null 2>&1; then
    echo "WARNING: Gateway does not appear to be running at $GATEWAY_URL"
    echo "Please start it first: uv run gateway"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create results directory
mkdir -p "$RESULTS_DIR"

# Start mock LLM server
echo "[3/6] Starting mock LLM server..."
python "$BENCHMARK_DIR/scripts/mock_llm_server.py" &
MOCK_PID=$!
echo "Mock server PID: $MOCK_PID"

# Wait for mock server to be ready
echo "Waiting for mock server to be ready..."
for i in {1..10}; do
    if curl -s "http://127.0.0.1:$MOCK_PORT" > /dev/null 2>&1; then
        echo "Mock server is ready."
        break
    fi
    sleep 1
done

# Run benchmark
echo ""
echo "[4/6] Running benchmark..."
echo "This will take approximately 2-5 minutes."
echo ""

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_FILE="$RESULTS_DIR/benchmark_$TIMESTAMP.json"

python "$BENCHMARK_DIR/scripts/benchmark.py" \
    --experiment latency \
    --gateway-url "$GATEWAY_URL" \
    --output "$RESULT_FILE"

# Generate report
echo ""
echo "[5/6] Generating report..."

REPORT_FILE="$BENCHMARK_DIR/BENCHMARK_REPORT.md"
python "$BENCHMARK_DIR/scripts/generate_report.py" \
    --input "$RESULTS_DIR" \
    --output "$REPORT_FILE"

# Cleanup
echo ""
echo "[6/6] Cleaning up..."
kill $MOCK_PID 2>/dev/null || true
wait $MOCK_PID 2>/dev/null || true

echo ""
echo "========================================"
echo "Benchmark Complete!"
echo "========================================"
echo ""
echo "Results:"
echo "  Raw data:   $RESULT_FILE"
echo "  Report:     $REPORT_FILE"
echo ""
echo "Next steps:"
echo "  1. Review the report: cat $REPORT_FILE"
echo "  2. Update interview docs with real numbers"
echo "  3. Run 'python $BENCHMARK_DIR/scripts/benchmark.py --experiment streaming' for TTFT data"
echo ""
