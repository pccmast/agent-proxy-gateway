"""Agent Gateway Phase 2 Benchmark v2 — Real GPU Backend (lm studio).

Supports:
- Baseline, light concurrency, stair-step load tests
- Streaming and non-streaming modes
- Client-side observability (connection timing, chunk intervals)
- JSON result export

Usage:
    .venv/Scripts/python.exe benchmark_v2/scripts/benchmark_v2.py --all
    .venv/Scripts/python.exe benchmark_v2/scripts/benchmark_v2.py --experiment e1

Author: Phase 2 benchmark team
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


# --------------------------------------------------------------------------- config

@dataclass
class BackendConfig:
    base_url: str
    api_key: str
    model: str
    backend_type: str = "lm_studio"
    max_tokens: int = 128
    temperature: float = 0.1


@dataclass
class ExperimentConfig:
    name: str
    concurrency: int
    requests: int
    streaming: bool
    prompt: str
    duration: int | None = None  # for stair-step


@dataclass
class StairConfig:
    steps: list[dict[str, Any]]
    step_interval: int
    streaming: bool
    prompt: str


@dataclass
class BenchmarkConfig:
    backend: BackendConfig
    gateway: dict[str, Any]
    experiments: dict[str, Any]
    observability: dict[str, Any]
    output: dict[str, Any]


# --------------------------------------------------------------------------- metrics

@dataclass
class RequestMetrics:
    experiment: str
    idx: int
    latency_ms: float = 0.0
    error: str = ""
    # Timing breakdown
    t_start: float = 0.0
    t_connect: float = 0.0
    t_first_byte: float = 0.0
    t_complete: float = 0.0
    # Derived
    connect_time_ms: float = 0.0
    ttft_ms: float = 0.0
    generation_time_ms: float = 0.0
    # Streaming
    chunk_count: int = 0
    chunk_intervals_ms: list[float] = field(default_factory=list)
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# --------------------------------------------------------------------------- helpers


def _now() -> float:
    return time.perf_counter()


def _ms(dt: float) -> float:
    return dt * 1000.0


def _load_config(path: Path) -> BenchmarkConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    backend_raw = dict(raw["backend"])
    # Rename 'type' to 'backend_type' to avoid keyword conflict
    if "type" in backend_raw:
        backend_raw["backend_type"] = backend_raw.pop("type")
    return BenchmarkConfig(
        backend=BackendConfig(**backend_raw),
        gateway=raw["gateway"],
        observability=raw.get("observability", {}),
        output=raw.get("output", {}),
        experiments=raw.get("experiments", {}),
    )


def _make_payload(cfg: BackendConfig, prompt: str, stream: bool) -> dict[str, Any]:
    return {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
        "stream": stream,
    }


def _headers(cfg: BackendConfig) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(len(sorted_vals) * p / 100.0)
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


def _stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0, "avg": 0.0}
    s = sorted(vals)
    return {
        "p50": _percentile(s, 50),
        "p95": _percentile(s, 95),
        "p99": _percentile(s, 99),
        "min": s[0],
        "max": s[-1],
        "avg": sum(s) / len(s),
    }


# --------------------------------------------------------------------------- request handlers


async def _request_non_streaming(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    experiment: str,
    idx: int,
) -> RequestMetrics:
    m = RequestMetrics(experiment=experiment, idx=idx)
    t0 = _now()
    m.t_start = t0

    try:
        resp = await client.post(url, json=payload, headers=headers, timeout=120.0)
        resp.raise_for_status()
    except Exception as e:
        m.t_complete = _now()
        m.latency_ms = _ms(m.t_complete - t0)
        m.error = f"{type(e).__name__}: {str(e)[:120]}"
        return m

    t2 = _now()
    m.t_first_byte = t2
    m.t_complete = t2
    m.latency_ms = _ms(t2 - t0)

    # Parse token counts if available
    try:
        body = resp.json()
        if "usage" in body:
            m.prompt_tokens = body["usage"].get("prompt_tokens", 0)
            m.completion_tokens = body["usage"].get("completion_tokens", 0)
            m.total_tokens = body["usage"].get("total_tokens", 0)
    except Exception:
        pass

    return m


async def _request_streaming(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    experiment: str,
    idx: int,
    max_chunk_records: int = 20,
) -> RequestMetrics:
    m = RequestMetrics(experiment=experiment, idx=idx)
    t0 = _now()
    m.t_start = t0

    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=120.0) as resp:
            await resp.aread()
    except Exception as e:
        m.t_complete = _now()
        m.latency_ms = _ms(m.t_complete - t0)
        m.error = f"{type(e).__name__}: {str(e)[:120]}"
        return m

    t_first = None
    last_chunk_time = None
    chunk_times: list[float] = []

    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=120.0) as resp:
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                now = _now()
                if t_first is None:
                    t_first = now
                if last_chunk_time is not None:
                    interval = _ms(now - last_chunk_time)
                    if len(chunk_times) < max_chunk_records:
                        chunk_times.append(interval)
                last_chunk_time = now
                m.chunk_count += 1
    except Exception as e:
        m.t_complete = _now()
        m.latency_ms = _ms(m.t_complete - t0)
        m.error = f"{type(e).__name__}: {str(e)[:120]}"
        return m

    t3 = _now()
    m.t_complete = t3
    m.latency_ms = _ms(t3 - t0)
    if t_first is not None:
        m.t_first_byte = t_first
        m.ttft_ms = _ms(t_first - t0)
    m.generation_time_ms = _ms(t3 - (t_first or t0))
    m.chunk_intervals_ms = chunk_times

    return m


# --------------------------------------------------------------------------- experiment runners


async def _run_experiment(
    cfg: BenchmarkConfig,
    exp: ExperimentConfig,
    results: list[RequestMetrics],
) -> None:
    """Run a single experiment with fixed concurrency."""
    url = cfg.gateway["base_url"] + "/v1/chat/completions"
    headers = _headers(cfg.backend)
    payload = _make_payload(cfg.backend, exp.prompt, exp.streaming)

    async def _worker(idx: int) -> None:
        async with httpx.AsyncClient() as client:
            if exp.streaming:
                m = await _request_streaming(
                    client, url, headers, payload, exp.name, idx,
                    max_chunk_records=cfg.observability.get("max_chunk_intervals", 20),
                )
            else:
                m = await _request_non_streaming(client, url, headers, payload, exp.name, idx)
            results.append(m)

    semaphore = asyncio.Semaphore(exp.concurrency)
    async def _bounded(idx: int) -> None:
        async with semaphore:
            await _worker(idx)

    tasks = [asyncio.create_task(_bounded(i)) for i in range(exp.requests)]
    await asyncio.gather(*tasks)


async def _run_stair_step(
    cfg: BenchmarkConfig,
    stair: StairConfig,
    results: list[RequestMetrics],
) -> None:
    """Run stair-step experiment: increase concurrency per step."""
    url = cfg.gateway["base_url"] + "/v1/chat/completions"
    headers = _headers(cfg.backend)
    payload = _make_payload(cfg.backend, stair.prompt, stair.streaming)

    for step in stair.steps:
        concurrency = step["concurrency"]
        requests = step["requests"]
        duration = step.get("duration", 60)
        exp_name = f"stair_{concurrency}"

        print(f"\n  [Stair] Step {concurrency} concurrency, {requests} requests, ~{duration}s ...")

        step_results: list[RequestMetrics] = []
        async def _worker(idx: int) -> None:
            async with httpx.AsyncClient() as client:
                if stair.streaming:
                    m = await _request_streaming(
                        client, url, headers, payload, exp_name, idx,
                        max_chunk_records=cfg.observability.get("max_chunk_intervals", 20),
                    )
                else:
                    m = await _request_non_streaming(client, url, headers, payload, exp_name, idx)
                step_results.append(m)

        semaphore = asyncio.Semaphore(concurrency)
        async def _bounded(idx: int) -> None:
            async with semaphore:
                await _worker(idx)

        tasks = [asyncio.create_task(_bounded(i)) for i in range(requests)]
        await asyncio.gather(*tasks)
        results.extend(step_results)

        # Print step summary
        latencies = [r.latency_ms for r in step_results if not r.error]
        errors = [r for r in step_results if r.error]
        if latencies:
            stats = _stats(latencies)
            print(f"    -> P50={stats['p50']:.0f}ms, P95={stats['p95']:.0f}ms, QPS={len(latencies)/max(stats['avg'],1)*1000:.1f}, Errors={len(errors)}")
        else:
            print(f"    -> All errors: {len(errors)}/{len(step_results)}")

        # Step interval rest
        if stair.step_interval > 0 and step != stair.steps[-1]:
            print(f"    -> Resting {stair.step_interval}s ...")
            await asyncio.sleep(stair.step_interval)


# --------------------------------------------------------------------------- main


def _print_banner() -> None:
    print("=" * 60)
    print("AGENT GATEWAY PHASE 2 BENCHMARK v2")
    print("Real GPU Backend: GTX1650 + minicpm-v-4.6 Q8_0")
    print("=" * 60)


def _print_experiment_summary(name: str, results: list[RequestMetrics]) -> None:
    latencies = [r.latency_ms for r in results if not r.error]
    errors = [r for r in results if r.error]
    if not latencies:
        print(f"  [{name}] ALL FAILED ({len(errors)} errors)")
        return

    stats = _stats(latencies)
    total_time = max(r.t_complete for r in results) - min(r.t_start for r in results)
    qps = len(latencies) / total_time if total_time > 0 else 0.0

    print(f"\n  [{name}] Results:")
    print(f"    Requests: {len(latencies)} success, {len(errors)} error")
    print(f"    P50: {stats['p50']:.0f}ms | P95: {stats['p95']:.0f}ms | P99: {stats['p99']:.0f}ms")
    print(f"    Min: {stats['min']:.0f}ms | Max: {stats['max']:.0f}ms | Avg: {stats['avg']:.0f}ms")
    print(f"    QPS: {qps:.2f}")
    if errors:
        print(f"    Sample errors: {[e.error[:80] for e in errors[:3]]}")

    # Streaming breakdown
    streaming_results = [r for r in results if r.chunk_count > 0]
    if streaming_results:
        ttfts = [r.ttft_ms for r in streaming_results if r.ttft_ms > 0]
        if ttfts:
            ttft_stats = _stats(ttfts)
            print(f"    Streaming TTFT: P50={ttft_stats['p50']:.0f}ms, P95={ttft_stats['p95']:.0f}ms")
        chunk_counts = [r.chunk_count for r in streaming_results]
        if chunk_counts:
            avg_chunks = sum(chunk_counts) / len(chunk_counts)
            print(f"    Avg chunks/response: {avg_chunks:.1f}")


def _save_results(results: list[RequestMetrics], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"benchmark_v2_{timestamp}.json"

    serializable = []
    for r in results:
        d = asdict(r)
        # Round floats for readability
        for k in ["latency_ms", "connect_time_ms", "ttft_ms", "generation_time_ms"]:
            if k in d:
                d[k] = round(d[k], 2)
        d["chunk_intervals_ms"] = [round(x, 2) for x in d.get("chunk_intervals_ms", [])]
        serializable.append(d)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(serializable, fh, indent=2, ensure_ascii=False)

    return out_path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Gateway Phase 2 Benchmark v2")
    parser.add_argument("--config", default="benchmark_v2/config.yaml", help="Config file path")
    parser.add_argument("--experiment", default="all", help="Experiment name or 'all'")
    parser.add_argument("--output", default="benchmark_v2/results", help="Output directory")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"ERROR: Config not found: {cfg_path}")
        sys.exit(1)

    cfg = _load_config(cfg_path)
    output_dir = Path(args.output)

    _print_banner()
    print(f"Config: {cfg_path}")
    print(f"Backend: {cfg.backend.model} @ {cfg.backend.base_url}")
    print(f"Gateway: {cfg.gateway['base_url']} (mode={cfg.gateway.get('test_mode', 'gateway')})")
    print(f"Output:  {output_dir}")

    all_results: list[RequestMetrics] = []

    # Run experiments
    experiments = cfg.experiments
    run_all = args.experiment == "all"

    # E1: Baseline
    if run_all or args.experiment == "e1":
        e1 = experiments.get("e1_baseline", {})
        if e1:
            print(f"\n{'='*60}")
            print("E1: Baseline (1 concurrent, 5 requests)")
            print(f"{'='*60}")
            exp = ExperimentConfig(name="e1_baseline", concurrency=e1["concurrency"], requests=e1["requests"], streaming=e1["streaming"], prompt=e1["prompt"])
            results: list[RequestMetrics] = []
            await _run_experiment(cfg, exp, results)
            all_results.extend(results)
            _print_experiment_summary("E1 Baseline", results)

    # E2: Light concurrency
    if run_all or args.experiment == "e2":
        e2 = experiments.get("e2_light_concurrency", {})
        if e2:
            print(f"\n{'='*60}")
            print("E2: Light Concurrency (3 concurrent, 10 requests)")
            print(f"{'='*60}")
            exp = ExperimentConfig(name="e2_light", concurrency=e2["concurrency"], requests=e2["requests"], streaming=e2["streaming"], prompt=e2["prompt"])
            results = []
            await _run_experiment(cfg, exp, results)
            all_results.extend(results)
            _print_experiment_summary("E2 Light Concurrency", results)

    # E3: Stair step
    if run_all or args.experiment == "e3":
        e3 = experiments.get("e3_stair_step", {})
        if e3:
            print(f"\n{'='*60}")
            print("E3: Stair Step Load (5 -> 10 -> 20 concurrent)")
            print(f"{'='*60}")
            stair = StairConfig(steps=e3["steps"], step_interval=e3["step_interval"], streaming=e3["streaming"], prompt=e3["prompt"])
            results = []
            await _run_stair_step(cfg, stair, results)
            all_results.extend(results)
            _print_experiment_summary("E3 Stair Step", results)

    # E4: Streaming baseline
    if run_all or args.experiment == "e4":
        e4 = experiments.get("e4_streaming_baseline", {})
        if e4:
            print(f"\n{'='*60}")
            print("E4: Streaming Baseline (1 concurrent, 5 requests)")
            print(f"{'='*60}")
            exp = ExperimentConfig(name="e4_streaming", concurrency=e4["concurrency"], requests=e4["requests"], streaming=e4["streaming"], prompt=e4["prompt"])
            results = []
            await _run_experiment(cfg, exp, results)
            all_results.extend(results)
            _print_experiment_summary("E4 Streaming Baseline", results)

    # E5: Streaming concurrency
    if run_all or args.experiment == "e5":
        e5 = experiments.get("e5_streaming_concurrency", {})
        if e5:
            print(f"\n{'='*60}")
            print("E5: Streaming Concurrency (3 concurrent, 6 requests)")
            print(f"{'='*60}")
            exp = ExperimentConfig(name="e5_streaming_con", concurrency=e5["concurrency"], requests=e5["requests"], streaming=e5["streaming"], prompt=e5["prompt"])
            results = []
            await _run_experiment(cfg, exp, results)
            all_results.extend(results)
            _print_experiment_summary("E5 Streaming Concurrency", results)

    # Save
    out_path = _save_results(all_results, output_dir)
    print(f"\n{'='*60}")
    print(f"All results saved to: {out_path}")
    print(f"Total requests: {len(all_results)}")
    print(f"Success: {len([r for r in all_results if not r.error])}")
    print(f"Errors:  {len([r for r in all_results if r.error])}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
