"""Gateway benchmark client — measures latency, throughput, and resource usage.

Supports three experiment types:
  1. latency    — Non-streaming request latency at various concurrency levels
  2. streaming  — Streaming (SSE) TTFT (Time to First Token) benchmark
  3. breakdown  — Per-module latency decomposition (requires instrumented gateway)

Usage:
    # Experiment A: Latency benchmark
    python benchmark/scripts/benchmark.py --experiment latency --output results/latency.json

    # Experiment B: Streaming TTFT
    python benchmark/scripts/benchmark.py --experiment streaming --output results/streaming.json

    # Experiment C: Module breakdown (requires gateway with profiling hooks)
    python benchmark/scripts/benchmark.py --experiment breakdown --output results/breakdown.json

Prerequisites:
    1. Gateway running: uv run gateway
    2. Mock LLM server: python benchmark/scripts/mock_llm_server.py
    3. config/default.yaml: openai base_url set to http://127.0.0.1:18081
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Callable

import httpx


# ──────────────────────────────────────────────────────────────────────────────
# Service management (auto-start mode)
# ──────────────────────────────────────────────────────────────────────────────

_gateway_proc: subprocess.Popen | None = None
_mock_proc: subprocess.Popen | None = None


def _wait_for_service(url: str, timeout: int = 30, method: str = "GET", body: str = "") -> bool:
    """Wait for a service to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, data=body.encode() if body else None, method=method)
            if body:
                req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=1)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError):
            time.sleep(0.5)
    return False


def start_services() -> bool:
    """Start mock server and gateway in background subprocesses."""
    project_root = Path(__file__).parent.parent.parent.resolve()
    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        print(f"ERROR: Python not found at {python_exe}")
        return False

    global _mock_proc, _gateway_proc

    print("[auto-start] Starting Mock LLM Server...")
    _mock_proc = subprocess.Popen(
        [str(python_exe), str(project_root / "benchmark" / "scripts" / "mock_llm_server.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_service("http://127.0.0.1:18081/v1/chat/completions", timeout=10, method="POST", body='{"model":"m"}'):
        print("ERROR: Mock server failed to start")
        return False
    print("[auto-start] Mock server ready")

    print("[auto-start] Starting Gateway...")
    _gateway_proc = subprocess.Popen(
        [str(python_exe), "-m", "gateway.main"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_service("http://127.0.0.1:18080/health", timeout=15):
        print("ERROR: Gateway failed to start")
        return False
    print("[auto-start] Gateway ready")
    return True


def stop_services() -> None:
    """Stop background services."""
    global _gateway_proc, _mock_proc
    if _gateway_proc:
        print("[auto-start] Stopping gateway...")
        _gateway_proc.terminate()
        try:
            _gateway_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _gateway_proc.kill()
        _gateway_proc = None
    if _mock_proc:
        print("[auto-start] Stopping mock server...")
        _mock_proc.terminate()
        try:
            _mock_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _mock_proc.kill()
        _mock_proc = None


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LatencyResult:
    concurrency: int
    total_requests: int
    total_duration_ms: float
    qps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    errors: int
    error_rate: float
    raw_latencies: list[float] = field(repr=False)


@dataclass
class StreamingResult:
    requests: int
    ttft_p50_ms: float
    ttft_p95_ms: float
    ttft_p99_ms: float
    total_p50_ms: float
    total_p95_ms: float
    total_p99_ms: float
    raw_ttft: list[float] = field(repr=False)
    raw_total: list[float] = field(repr=False)


@dataclass
class BreakdownResult:
    concurrency: int
    requests: int
    adapter_normalize_ms: float
    middleware_request_ms: float
    upstream_forward_ms: float
    middleware_response_ms: float
    trace_finish_ms: float
    total_ms: float


@dataclass
class BenchmarkOutput:
    experiment: str
    timestamp: str
    hardware: dict[str, Any]
    config: dict[str, Any]
    results: list[dict[str, Any]]


# ──────────────────────────────────────────────────────────────────────────────
# Core benchmark functions
# ──────────────────────────────────────────────────────────────────────────────

async def _single_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[float, str]:
    """Execute one request and return (latency_ms, error_msg). latency=-1.0 on error."""
    start = time.perf_counter()
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=30.0)
        resp.raise_for_status()
    except Exception as e:
        return -1.0, f"{type(e).__name__}: {str(e)[:100]}"
    return (time.perf_counter() - start) * 1000.0, ""


async def _single_streaming_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[float, float]:
    """Execute one streaming request and return (ttft_ms, total_ms).

    TTFT = Time to First Token = time until first SSE data line received.
    Returns (-1.0, -1.0) on error.
    """
    total_start = time.perf_counter()
    first_token_time: float | None = None

    try:
        async with client.stream(
            "POST", url, json=body, headers=headers, timeout=30.0
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: ") and first_token_time is None:
                    first_token_time = time.perf_counter()
                    # We have TTFT, but continue consuming to get total time
                if line.startswith("data: [DONE]"):
                    break
    except Exception:
        return -1.0, -1.0

    total_end = time.perf_counter()
    ttft = (first_token_time - total_start) * 1000.0 if first_token_time else 0.0
    total = (total_end - total_start) * 1000.0
    return ttft, total


async def run_latency_benchmark(
    concurrency: int,
    total_requests: int,
    gateway_url: str,
    warmup: int = 5,
) -> LatencyResult:
    """Run non-streaming latency benchmark at a specific concurrency level."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer mock-key",
        "X-Agent-ID": "benchmark-agent",
    }
    body: dict[str, Any] = {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "Hello, benchmark test."}],
        "stream": False,
    }
    url = f"{gateway_url}/v1/chat/completions"

    limits = httpx.Limits(max_connections=concurrency * 2)
    async with httpx.AsyncClient(http2=True, limits=limits) as client:
        # Warmup
        for _ in range(warmup):
            await _single_request(client, url, headers, body)

        # Actual benchmark
        latencies: list[float] = []
        errors = 0
        error_details: list[str] = []
        start_time = time.perf_counter()

        for batch_start in range(0, total_requests, concurrency):
            batch_size = min(concurrency, total_requests - batch_start)
            tasks = [_single_request(client, url, headers, body) for _ in range(batch_size)]
            batch_results = await asyncio.gather(*tasks)
            for latency, err_msg in batch_results:
                if latency < 0:
                    errors += 1
                    if err_msg and len(error_details) < 3:
                        error_details.append(err_msg)
                else:
                    latencies.append(latency)

        total_duration = (time.perf_counter() - start_time) * 1000.0

        # Print first few errors for debugging
        if error_details:
            print(f"  Sample errors: {error_details}")

    # Statistics
    latencies.sort()
    n = len(latencies)
    if n == 0:
        return LatencyResult(
            concurrency=concurrency,
            total_requests=total_requests,
            total_duration_ms=round(total_duration, 2),
            qps=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            min_ms=0.0,
            max_ms=0.0,
            errors=errors,
            error_rate=1.0,
            raw_latencies=[],
        )

    return LatencyResult(
        concurrency=concurrency,
        total_requests=total_requests,
        total_duration_ms=round(total_duration, 2),
        qps=round(total_requests / (total_duration / 1000.0), 2),
        p50_ms=round(latencies[n // 2], 2),
        p95_ms=round(latencies[int(n * 0.95)], 2),
        p99_ms=round(latencies[int(n * 0.99)], 2),
        min_ms=round(latencies[0], 2),
        max_ms=round(latencies[-1], 2),
        errors=errors,
        error_rate=round(errors / total_requests, 4),
        raw_latencies=latencies,
    )


async def run_streaming_benchmark(
    num_requests: int,
    gateway_url: str,
    warmup: int = 3,
) -> StreamingResult:
    """Run streaming TTFT benchmark."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer mock-key",
    }
    body: dict[str, Any] = {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "Stream benchmark test."}],
        "stream": True,
    }
    url = f"{gateway_url}/v1/chat/completions"

    async with httpx.AsyncClient(http2=True) as client:
        # Warmup
        for _ in range(warmup):
            await _single_streaming_request(client, url, headers, body)

        # Actual benchmark
        ttft_list: list[float] = []
        total_list: list[float] = []

        for _ in range(num_requests):
            ttft, total = await _single_streaming_request(client, url, headers, body)
            if ttft >= 0:
                ttft_list.append(ttft)
                total_list.append(total)

    ttft_list.sort()
    total_list.sort()
    n = len(ttft_list)
    if n == 0:
        return StreamingResult(
            requests=num_requests,
            ttft_p50_ms=0.0,
            ttft_p95_ms=0.0,
            ttft_p99_ms=0.0,
            total_p50_ms=0.0,
            total_p95_ms=0.0,
            total_p99_ms=0.0,
            raw_ttft=[],
            raw_total=[],
        )

    return StreamingResult(
        requests=n,
        ttft_p50_ms=round(statistics.median(ttft_list), 2),
        ttft_p95_ms=round(ttft_list[int(n * 0.95)], 2),
        ttft_p99_ms=round(ttft_list[int(n * 0.99)], 2),
        total_p50_ms=round(statistics.median(total_list), 2),
        total_p95_ms=round(total_list[int(n * 0.95)], 2),
        total_p99_ms=round(total_list[int(n * 0.99)], 2),
        raw_ttft=ttft_list,
        raw_total=total_list,
    )


async def run_breakdown_benchmark(
    num_requests: int,
    gateway_url: str,
) -> list[BreakdownResult]:
    """Run module breakdown benchmark.

    NOTE: This requires the gateway to expose profiling headers or an internal
    profiling endpoint. If not available, falls back to estimating from total latency.
    """
    # For now, this is a placeholder that runs the same requests but with
    # the assumption that the gateway will add X-Profile-* headers.
    # In a real implementation, the gateway would be instrumented with
    # time.perf_counter() at each phase.
    print("WARNING: Module breakdown requires instrumented gateway.")
    print("Falling back to total latency measurement.")

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer mock-key",
        "X-Agent-ID": "benchmark-agent",
        "X-Enable-Profiling": "true",  # Request profiling if gateway supports it
    }
    body: dict[str, Any] = {
        "model": "mock-model",
        "messages": [{"role": "user", "content": "Breakdown test."}],
        "stream": False,
    }
    url = f"{gateway_url}/v1/chat/completions"

    results: list[BreakdownResult] = []
    async with httpx.AsyncClient(http2=True) as client:
        for _ in range(num_requests):
            start = time.perf_counter()
            try:
                resp = await client.post(url, json=body, headers=headers, timeout=30.0)
                resp.raise_for_status()
                total_ms = (time.perf_counter() - start) * 1000.0

                # Check for profiling headers
                profile = resp.headers.get("X-Profile-Data", "")
                if profile:
                    try:
                        data = json.loads(profile)
                        results.append(
                            BreakdownResult(
                                concurrency=1,
                                requests=1,
                                adapter_normalize_ms=data.get("adapter", 0.0),
                                middleware_request_ms=data.get("middleware_request", 0.0),
                                upstream_forward_ms=data.get("upstream", 0.0),
                                middleware_response_ms=data.get("middleware_response", 0.0),
                                trace_finish_ms=data.get("trace", 0.0),
                                total_ms=total_ms,
                            )
                        )
                    except json.JSONDecodeError:
                        results.append(
                            BreakdownResult(
                                concurrency=1,
                                requests=1,
                                adapter_normalize_ms=0.0,
                                middleware_request_ms=0.0,
                                upstream_forward_ms=0.0,
                                middleware_response_ms=0.0,
                                trace_finish_ms=0.0,
                                total_ms=total_ms,
                            )
                        )
                else:
                    results.append(
                        BreakdownResult(
                            concurrency=1,
                            requests=1,
                            adapter_normalize_ms=0.0,
                            middleware_request_ms=0.0,
                            upstream_forward_ms=0.0,
                            middleware_response_ms=0.0,
                            trace_finish_ms=0.0,
                            total_ms=total_ms,
                        )
                    )
            except Exception:
                pass

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Output and reporting
# ──────────────────────────────────────────────────────────────────────────────

def _get_hardware_info() -> dict[str, Any]:
    """Collect hardware and environment information."""
    return {
        "cpu": platform.processor() or platform.machine(),
        "machine": platform.machine(),
        "system": platform.system(),
        "release": platform.release(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }


def _save_results(output_path: str, data: BenchmarkOutput) -> None:
    """Save benchmark results to JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(data), f, indent=2, ensure_ascii=False)
    print(f"Results saved to: {path}")


def _print_latency_table(results: list[LatencyResult]) -> None:
    """Print formatted latency results table."""
    print("\n" + "=" * 90)
    print("NON-STREAMING LATENCY BENCHMARK")
    print("=" * 90)
    print(
        f"{'Concurrency':>12} | {'Requests':>8} | {'QPS':>8} | "
        f"{'P50(ms)':>8} | {'P95(ms)':>8} | {'P99(ms)':>8} | "
        f"{'Min(ms)':>8} | {'Max(ms)':>8} | {'Errors':>6}"
    )
    print("-" * 90)
    for r in results:
        print(
            f"{r.concurrency:>12} | {r.total_requests:>8} | {r.qps:>8} | "
            f"{r.p50_ms:>8} | {r.p95_ms:>8} | {r.p99_ms:>8} | "
            f"{r.min_ms:>8} | {r.max_ms:>8} | {r.errors:>6}"
        )


def _print_streaming_table(result: StreamingResult) -> None:
    """Print formatted streaming results."""
    print("\n" + "=" * 60)
    print("STREAMING (SSE) TTFT BENCHMARK")
    print("=" * 60)
    print(f"Requests:        {result.requests}")
    print(f"TTFT P50:        {result.ttft_p50_ms} ms")
    print(f"TTFT P95:        {result.ttft_p95_ms} ms")
    print(f"TTFT P99:        {result.ttft_p99_ms} ms")
    print(f"Total Time P50:  {result.total_p50_ms} ms")
    print(f"Total Time P95:  {result.total_p95_ms} ms")
    print(f"Total Time P99:  {result.total_p99_ms} ms")


def _print_breakdown_table(results: list[BreakdownResult]) -> None:
    """Print formatted breakdown results."""
    print("\n" + "=" * 80)
    print("MODULE BREAKDOWN BENCHMARK")
    print("=" * 80)
    if not results:
        print("No results collected.")
        return

    # Average across all requests
    avg = BreakdownResult(
        concurrency=1,
        requests=len(results),
        adapter_normalize_ms=round(
            statistics.mean([r.adapter_normalize_ms for r in results]), 2
        ),
        middleware_request_ms=round(
            statistics.mean([r.middleware_request_ms for r in results]), 2
        ),
        upstream_forward_ms=round(
            statistics.mean([r.upstream_forward_ms for r in results]), 2
        ),
        middleware_response_ms=round(
            statistics.mean([r.middleware_response_ms for r in results]), 2
        ),
        trace_finish_ms=round(
            statistics.mean([r.trace_finish_ms for r in results]), 2
        ),
        total_ms=round(statistics.mean([r.total_ms for r in results]), 2),
    )

    print(f"{'Phase':<25} | {'Avg(ms)':>10} | {'% of Total':>12}")
    print("-" * 50)
    total = avg.total_ms or 1.0  # avoid div by zero
    phases = [
        ("Adapter.normalize_request", avg.adapter_normalize_ms),
        ("Middleware.on_request", avg.middleware_request_ms),
        ("Upstream.forward", avg.upstream_forward_ms),
        ("Middleware.on_response", avg.middleware_response_ms),
        ("Trace.finish_span", avg.trace_finish_ms),
    ]
    for name, val in phases:
        pct = (val / total) * 100 if total > 0 else 0
        print(f"{name:<25} | {val:>10.2f} | {pct:>11.1f}%")
    print("-" * 50)
    print(f"{'TOTAL':<25} | {avg.total_ms:>10.2f} | {'100.0%':>12}")


def _print_interview_takeaways(latency_results: list[LatencyResult], streaming: StreamingResult) -> None:
    """Print key takeaways formatted for interview use."""
    print("\n" + "=" * 60)
    print("KEY TAKEAWAYS FOR INTERVIEW")
    print("=" * 60)

    if latency_results:
        best = min(latency_results, key=lambda x: x.p50_ms)
        peak = max(latency_results, key=lambda x: x.qps)
        high = next((r for r in latency_results if r.concurrency >= 100), None)

        print(f"* Single-request P50 latency: {best.p50_ms} ms")
        print(f"  (This is the 'gateway overhead' for one request)")
        print()
        print(f"* Peak throughput: {peak.qps} QPS @ {peak.concurrency} concurrency")
        print()
        if high:
            print(f"* At 100 concurrent requests:")
            print(f"  - P50 latency: {high.p50_ms} ms")
            print(f"  - P95 latency: {high.p95_ms} ms")
            print(f"  - Error rate: {high.error_rate * 100:.2f}%")
            print()
            # Bottleneck analysis
            p95_jump = high.p95_ms / best.p95_ms if best.p95_ms > 0 else 0
            if p95_jump > 3:
                print(f"* Bottleneck identified: P95 latency increased {p95_jump:.1f}x")
                print(f"  from {best.concurrency} to {high.concurrency} concurrency.")
                print(f"  Primary suspect: SQLite write lock contention.")

    if streaming.requests > 0:
        print(f"* Streaming TTFT P50: {streaming.ttft_p50_ms} ms")
        print(f"  (Time from request to first SSE chunk)")

    print()
    print("Sample interview answer:")
    print('  "I ran benchmarks with a mock upstream to isolate gateway')
    print('   performance. Single-request P50 is ~{:.0f}ms, and at 100')
    print('   concurrency P95 rises to ~{:.0f}ms due to SQLite lock')
    print('   contention. The optimization path is WAL mode first,'.format(
        best.p50_ms if latency_results else 0,
        high.p95_ms if high else 0,
    ))
    print('   then PostgreSQL migration."')


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Gateway benchmark client")
    parser.add_argument(
        "--experiment",
        choices=["latency", "streaming", "breakdown"],
        default="latency",
        help="Type of benchmark to run (default: latency)",
    )
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:18080",
        help="Gateway URL (default: http://127.0.0.1:18080)",
    )
    parser.add_argument(
        "--output",
        default="benchmark/results/result.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--latency-concurrency",
        nargs="+",
        type=int,
        default=[1, 10, 50, 100, 200],
        help="Concurrency levels for latency benchmark (default: 1 10 50 100 200)",
    )
    parser.add_argument(
        "--latency-requests",
        type=int,
        default=300,
        help="Total requests per concurrency level (default: 300)",
    )
    parser.add_argument(
        "--streaming-requests",
        type=int,
        default=20,
        help="Number of streaming requests (default: 20)",
    )
    parser.add_argument(
        "--breakdown-requests",
        type=int,
        default=10,
        help="Number of breakdown requests (default: 10)",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Auto-start mock server and gateway before benchmark",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("AGENT PROXY GATEWAY BENCHMARK")
    print("=" * 60)
    print(f"Experiment:    {args.experiment}")
    print(f"Gateway URL:   {args.gateway_url}")
    print(f"Output file:   {args.output}")
    print(f"Auto-start:    {args.auto_start}")
    print()

    if args.auto_start:
        if not start_services():
            print("Failed to start services. Exiting.")
            return

    hardware = _get_hardware_info()
    print("Hardware info:")
    for k, v in hardware.items():
        print(f"  {k}: {v}")
    print()

    config = {
        "gateway_url": args.gateway_url,
        "experiment": args.experiment,
    }

    results_data: list[dict[str, Any]] = []

    if args.experiment == "latency":
        latency_results: list[LatencyResult] = []
        for concurrency in args.latency_concurrency:
            total = max(args.latency_requests, concurrency * 2)
            print(f"Running latency benchmark: concurrency={concurrency}, total_requests={total} ...")
            result = await run_latency_benchmark(
                concurrency=concurrency,
                total_requests=total,
                gateway_url=args.gateway_url,
            )
            latency_results.append(result)
            print(
                f"  Done: P50={result.p50_ms}ms, P95={result.p95_ms}ms, "
                f"QPS={result.qps}, Errors={result.errors}"
            )

        _print_latency_table(latency_results)
        results_data = [asdict(r) for r in latency_results]

        # Also run streaming for combined report
        print("\nRunning streaming benchmark (20 requests) ...")
        streaming_result = await run_streaming_benchmark(
            num_requests=args.streaming_requests,
            gateway_url=args.gateway_url,
        )
        _print_streaming_table(streaming_result)

        _print_interview_takeaways(latency_results, streaming_result)

        # Save combined output
        output = BenchmarkOutput(
            experiment="latency+streaming",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            hardware=hardware,
            config=config,
            results=results_data + [asdict(streaming_result)],
        )
        _save_results(args.output, output)

    elif args.experiment == "streaming":
        print(f"Running streaming benchmark: {args.streaming_requests} requests ...")
        streaming_result = await run_streaming_benchmark(
            num_requests=args.streaming_requests,
            gateway_url=args.gateway_url,
        )
        _print_streaming_table(streaming_result)

        output = BenchmarkOutput(
            experiment="streaming",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            hardware=hardware,
            config=config,
            results=[asdict(streaming_result)],
        )
        _save_results(args.output, output)

    elif args.experiment == "breakdown":
        print(f"Running breakdown benchmark: {args.breakdown_requests} requests ...")
        print("(NOTE: Requires instrumented gateway with profiling headers)")
        breakdown_results = await run_breakdown_benchmark(
            num_requests=args.breakdown_requests,
            gateway_url=args.gateway_url,
        )
        _print_breakdown_table(breakdown_results)

        output = BenchmarkOutput(
            experiment="breakdown",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            hardware=hardware,
            config=config,
            results=[asdict(r) for r in breakdown_results],
        )
        _save_results(args.output, output)

    print("\nBenchmark complete.")

    if args.auto_start:
        stop_services()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        stop_services()
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        stop_services()
        sys.exit(1)
