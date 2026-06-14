"""Benchmark launcher — starts mock server, starts gateway, runs benchmark, generates report, cleans up.

Usage:
    python benchmark/scripts/launcher.py
"""

import subprocess
import sys
import time
import urllib.request
import urllib.error
import signal
import os
from pathlib import Path


def wait_for_service(url: str, timeout: int = 30, method: str = "GET", body: str = "") -> bool:
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


def main() -> int:
    project_root = Path(__file__).parent.parent.parent.resolve()
    os.chdir(project_root)

    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        print(f"ERROR: Python not found at {python_exe}")
        return 1

    print("=" * 60)
    print("AGENT PROXY GATEWAY BENCHMARK LAUNCHER")
    print("=" * 60)
    print()

    # 1. Start Mock LLM Server
    print("[1/5] Starting Mock LLM Server...")
    mock_proc = subprocess.Popen(
        [str(python_exe), str(project_root / "benchmark" / "scripts" / "mock_llm_server.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  Mock server PID: {mock_proc.pid}")
    if not wait_for_service("http://127.0.0.1:18081/v1/chat/completions", timeout=10, method="POST", body='{"model":"m","messages":[{"role":"user","content":"hi"}]}'):
        print("ERROR: Mock server failed to start")
        mock_proc.terminate()
        return 1
    print("  Mock server ready")

    # 2. Start Gateway
    print("[2/5] Starting Gateway...")
    gateway_proc = subprocess.Popen(
        [str(python_exe), "-m", "gateway.main"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  Gateway PID: {gateway_proc.pid}")
    if not wait_for_service("http://127.0.0.1:18080/health", timeout=15):
        print("ERROR: Gateway failed to start")
        gateway_proc.terminate()
        mock_proc.terminate()
        return 1
    print("  Gateway ready")

    # 3. Run Benchmark
    print()
    print("[3/5] Running benchmark (latency + streaming)...")
    print("  This will take approximately 2-5 minutes.")
    print()

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    result_file = project_root / "benchmark" / "results" / f"benchmark_{timestamp}.json"
    result_file.parent.mkdir(parents=True, exist_ok=True)

    benchmark_script = project_root / "benchmark" / "scripts" / "benchmark.py"
    bench_proc = subprocess.run(
        [str(python_exe), str(benchmark_script),
         "--experiment", "latency",
         "--gateway-url", "http://127.0.0.1:18080",
         "--output", str(result_file),
         "--latency-concurrency", "1", "10", "50", "100",
         "--latency-requests", "200"],
        capture_output=False,
        text=False,
    )

    if bench_proc.returncode != 0:
        print(f"WARNING: Benchmark exited with code {bench_proc.returncode}")
    else:
        print("  Benchmark completed successfully")

    # 4. Generate Report
    print()
    print("[4/5] Generating report...")
    report_file = project_root / "benchmark" / "BENCHMARK_REPORT.md"
    report_script = project_root / "benchmark" / "scripts" / "generate_report.py"
    subprocess.run(
        [str(python_exe), str(report_script),
         "--input", str(project_root / "benchmark" / "results"),
         "--output", str(report_file)],
        capture_output=False,
    )
    print(f"  Report saved to: {report_file}")

    # 5. Cleanup
    print()
    print("[5/5] Cleaning up...")
    print(f"  Stopping gateway (PID: {gateway_proc.pid})...")
    gateway_proc.terminate()
    try:
        gateway_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        gateway_proc.kill()

    print(f"  Stopping mock server (PID: {mock_proc.pid})...")
    mock_proc.terminate()
    try:
        mock_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mock_proc.kill()

    print()
    print("=" * 60)
    print("BENCHMARK COMPLETE")
    print("=" * 60)
    print()
    print(f"Results:")
    print(f"  Raw data: {result_file}")
    print(f"  Report:   {report_file}")
    print()
    print("Next steps:")
    print("  1. Read the report: type benchmark/BENCHMARK_REPORT.md")
    print("  2. Update interview docs with real numbers")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
