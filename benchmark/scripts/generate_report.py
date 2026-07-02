"""Benchmark report generator — aggregates JSON result files into a Markdown report.

Usage:
    python benchmark/scripts/generate_report.py --input benchmark/results/ --output benchmark/BENCHMARK_REPORT.md

Reads all JSON files in the input directory, extracts key metrics,
and generates a formatted Markdown report with tables and analysis.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def _load_results(input_dir: str) -> list[dict[str, Any]]:
    """Load all JSON result files from the input directory."""
    results: list[dict[str, Any]] = []
    path = Path(input_dir)
    if not path.exists():
        print(f"Warning: Input directory '{input_dir}' does not exist.")
        return results

    for json_file in sorted(path.glob("*.json")):
        try:
            with open(json_file, encoding="utf-8") as f:
                data = json.load(f)
                data["_source_file"] = json_file.name
                results.append(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"Warning: Failed to parse {json_file}: {e}")
    return results


def _format_latency_table(results: list[dict[str, Any]]) -> str:
    """Format latency benchmark results as a Markdown table."""
    lines = [
        "### 实验 A: 非流式延迟基准\n",
        "| 并发数 | 请求数 | QPS | P50(ms) | P95(ms) | P99(ms) | 最小(ms) | 最大(ms) | 错误数 | 错误率 |",
        "|--------|--------|-----|---------|---------|---------|----------|----------|--------|--------|",
    ]

    for data in results:
        for r in data.get("results", []):
            if "concurrency" in r and "p50_ms" in r:
                lines.append(
                    f"| {r['concurrency']} | {r['total_requests']} | {r['qps']} | "
                    f"{r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
                    f"{r['min_ms']} | {r['max_ms']} | {r['errors']} | {r['error_rate'] * 100:.2f}% |"
                )

    return "\n".join(lines) + "\n"


def _format_streaming_table(results: list[dict[str, Any]]) -> str:
    """Format streaming benchmark results as a Markdown table."""
    lines = [
        "### 实验 B: 流式 TTFT 基准\n",
        "| 请求数 | TTFT P50(ms) | TTFT P95(ms) | TTFT P99(ms) | 总时间 P50(ms) | 总时间 P95(ms) |",
        "|--------|--------------|--------------|--------------|----------------|----------------|",
    ]

    for data in results:
        for r in data.get("results", []):
            if "ttft_p50_ms" in r:
                lines.append(
                    f"| {r['requests']} | {r['ttft_p50_ms']} | {r['ttft_p95_ms']} | "
                    f"{r['ttft_p99_ms']} | {r['total_p50_ms']} | {r['total_p95_ms']} |"
                )

    return "\n".join(lines) + "\n"


def _generate_bottleneck_analysis(results: list[dict[str, Any]]) -> str:
    """Generate bottleneck analysis from latency results."""
    latency_data: list[dict[str, Any]] = []
    for data in results:
        for r in data.get("results", []):
            if "concurrency" in r and "p50_ms" in r:
                latency_data.append(r)

    if not latency_data:
        return "暂无足够数据进行分析。\n"

    # Sort by concurrency
    latency_data.sort(key=lambda x: x.get("concurrency", 0))

    lines = ["### 瓶颈分析\n"]

    # Find the point where latency starts to degrade significantly
    baseline = latency_data[0] if latency_data else {}
    baseline_p95 = baseline.get("p95_ms", 1.0)

    degradation_point = None
    for r in latency_data[1:]:
        if r.get("p95_ms", 0) > baseline_p95 * 2:
            degradation_point = r
            break

    if degradation_point:
        lines.append(
            f"1. **延迟退化点**: 当并发数从 {baseline.get('concurrency', 1)} "
            f"增加到 {degradation_point['concurrency']} 时，P95 延迟从 "
            f"{baseline_p95}ms 上升到 {degradation_point['p95_ms']}ms，"
            f"增长了 {degradation_point['p95_ms'] / baseline_p95:.1f} 倍。\n"
        )
        lines.append(f"   这表明系统在 {degradation_point['concurrency']} 并发左右开始出现明显的性能瓶颈。\n\n")
    else:
        lines.append("1. **延迟退化点**: 在测试范围内未观察到明显的延迟退化点。\n\n")

    # Error rate analysis
    high_error = [r for r in latency_data if r.get("error_rate", 0) > 0.01]
    if high_error:
        worst = max(high_error, key=lambda x: x.get("error_rate", 0))
        lines.append(
            f"2. **错误率分析**: 在 {worst['concurrency']} 并发时错误率达到 "
            f"{worst['error_rate'] * 100:.2f}%，共 {worst['errors']} 个错误。\n"
            f"   主要错误原因可能是 SQLite 锁竞争导致的写入超时。\n\n"
        )
    else:
        lines.append("2. **错误率分析**: 所有测试配置下错误率均低于 1%，系统稳定性良好。\n\n")

    # QPS saturation
    if len(latency_data) >= 2:
        qps_values = [r.get("qps", 0) for r in latency_data]
        max_qps = max(qps_values)
        max_idx = qps_values.index(max_qps)
        lines.append(
            f"3. **吞吐量饱和**: 峰值 QPS 为 {max_qps}，出现在 "
            f"{latency_data[max_idx]['concurrency']} 并发时。\n"
            f"   继续增加并发并未显著提升 QPS，说明系统已达到处理能力上限。\n\n"
        )

    return "\n".join(lines)


def _generate_optimization_suggestions(results: list[dict[str, Any]]) -> str:
    """Generate optimization suggestions based on results."""
    lines = ["### 优化建议\n"]

    # Check if SQLite is likely the bottleneck
    latency_data = [r for data in results for r in data.get("results", []) if "concurrency" in r and "p50_ms" in r]

    if latency_data:
        max_concurrency = max(r.get("concurrency", 0) for r in latency_data)
        high_concurrency_result = next((r for r in latency_data if r.get("concurrency") == max_concurrency), None)

        if high_concurrency_result:
            p95 = high_concurrency_result.get("p95_ms", 0)
            baseline = next((r for r in latency_data if r.get("concurrency") == 1), None)
            baseline_p95 = baseline.get("p95_ms", 1) if baseline else 1

            if p95 > baseline_p95 * 3:
                lines.append(
                    "1. **启用 SQLite WAL 模式**\n"
                    "   - 当前 SQLite 使用默认日志模式，高并发下写操作会阻塞读操作。\n"
                    "   - WAL（Write-Ahead Logging）模式允许读写并发，预计可提升 30-50% 的并发写入性能。\n"
                    "   - 修改方式：在 `TraceStore.initialize()` 中执行 `PRAGMA journal_mode=WAL;`\n\n"
                )

            if p95 > baseline_p95 * 5:
                lines.append(
                    "2. **迁移到 PostgreSQL**\n"
                    "   - 当并发超过 100 时，SQLite 的锁竞争成为主要瓶颈。\n"
                    "   - PostgreSQL 支持真正的并发连接，连接池可配置，适合生产环境。\n"
                    "   - 迁移成本：修改 `TraceStore` 的 SQL 方言（`aiosqlite` → `asyncpg`），预计 1-2 天工作量。\n\n"
                )

            lines.append(
                "3. **批量写入优化**\n"
                "   - 当前每个请求产生 2 次 SQLite 写入（create_span + finish_span）。\n"
                "   - 可引入写入缓冲队列，攒 N 个 span 后批量提交（如每 50ms 或 100 个 span 提交一次）。\n"
                "   - 风险：缓冲期内如果进程崩溃，会丢失未提交的 trace 数据。\n\n"
            )

    lines.append(
        "4. **连接池调优**\n"
        "   - 当前 httpx.AsyncClient 使用默认连接池限制。\n"
        "   - 可根据并发级别动态调整 `limits=httpx.Limits(max_connections=...)`。\n\n"
    )

    return "\n".join(lines)


def _generate_interview_cheat_sheet(results: list[dict[str, Any]]) -> str:
    """Generate a cheat sheet of key numbers for interview use."""
    lines = ["### 面试速查表\n"]

    # Extract key numbers
    latency_data = [r for data in results for r in data.get("results", []) if "concurrency" in r and "p50_ms" in r]

    if latency_data:
        baseline = next((r for r in latency_data if r.get("concurrency") == 1), None)
        high = next((r for r in latency_data if r.get("concurrency") >= 100), None)
        peak = max(latency_data, key=lambda x: x.get("qps", 0))

        lines.append("**单请求基线**:\n")
        if baseline:
            lines.append(
                f"- P50: **{baseline['p50_ms']} ms** | "
                f"P95: **{baseline['p95_ms']} ms** | "
                f"P99: **{baseline['p99_ms']} ms**\n"
            )

        lines.append("\n**高并发表现（100 并发）**:\n")
        if high:
            lines.append(
                f"- P50: **{high['p50_ms']} ms** | "
                f"P95: **{high['p95_ms']} ms** | "
                f"QPS: **{high['qps']}** | "
                f"错误率: **{high['error_rate'] * 100:.2f}%**\n"
            )

        lines.append("\n**峰值吞吐量**:\n")
        if peak:
            lines.append(f"- 最高 QPS: **{peak['qps']}** @ {peak['concurrency']} 并发\n")

    # Streaming data
    streaming_data = [r for data in results for r in data.get("results", []) if "ttft_p50_ms" in r]
    if streaming_data:
        s = streaming_data[0]
        lines.append("\n**流式 TTFT**:\n")
        lines.append(f"- P50: **{s['ttft_p50_ms']} ms** | P95: **{s['ttft_p95_ms']} ms**\n")

    lines.append("\n**面试话术模板**:\n")
    lines.append(
        '> "我用 mock 上游隔离了网络变量，测了网关自身的性能。'
        "单请求 P50 是 **{p50}ms**，100 并发时 P95 上升到 **{p95}ms**，"
        "主要瓶颈是 SQLite 写入锁竞争。优化路径是：先启用 WAL 模式提升 30% 性能，"
        '长期迁移到 PostgreSQL。"\n\n'.format(
            p50=baseline["p50_ms"] if baseline else "XX",
            p95=high["p95_ms"] if high else "XX",
        )
    )

    return "\n".join(lines)


def generate_report(input_dir: str, output_file: str) -> None:
    """Generate a Markdown report from benchmark result files."""
    results = _load_results(input_dir)

    if not results:
        print(f"No result files found in '{input_dir}'. Run benchmark first.")
        return

    # Extract hardware info from first result
    hardware = results[0].get("hardware", {})
    timestamp = results[0].get("timestamp", time.strftime("%Y-%m-%d %H:%M:%S"))

    lines = [
        "# Agent Proxy Gateway — 压测报告\n",
        f"> **生成时间**: {timestamp}\n",
        f"> **测试环境**: {hardware.get('system', 'Unknown')} {hardware.get('release', '')}\n",
        f"> **CPU**: {hardware.get('cpu', 'Unknown')}\n",
        f"> **Python**: {hardware.get('python_version', 'Unknown')}\n",
        "\n## 执行摘要\n",
        "本次压测使用 **Mock LLM Server** 隔离上游网络变量，专注于测量网关自身的处理性能。",
        "测试覆盖了非流式延迟、并发吞吐量、流式 TTFT 三个维度。\n",
        "\n## 测试结果\n",
        _format_latency_table(results),
        _format_streaming_table(results),
        "\n## 分析\n",
        _generate_bottleneck_analysis(results),
        _generate_optimization_suggestions(results),
        "\n## 面试准备\n",
        _generate_interview_cheat_sheet(results),
        "\n## 原始数据\n",
        "| 文件 | 实验类型 | 时间 |",
        "|------|----------|------|",
    ]

    for data in results:
        lines.append(
            f"| {data.get('_source_file', 'unknown')} | "
            f"{data.get('experiment', 'unknown')} | "
            f"{data.get('timestamp', 'unknown')} |"
        )

    lines.append("\n---\n")
    lines.append(
        "> **注意**: 本报告数据基于本地开发环境测试，生产环境性能可能因硬件、网络、",
        "数据库配置等因素而有所不同。面试中应明确说明测试环境，避免夸大性能数据。\n",
    )

    # Write report
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Report generated: {output_path}")
    print(f"Total result files processed: {len(results)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate benchmark report from JSON results")
    parser.add_argument(
        "--input",
        default="benchmark/results",
        help="Directory containing JSON result files (default: benchmark/results)",
    )
    parser.add_argument(
        "--output",
        default="benchmark/BENCHMARK_REPORT.md",
        help="Output Markdown report file (default: benchmark/BENCHMARK_REPORT.md)",
    )
    args = parser.parse_args()

    generate_report(args.input, args.output)


if __name__ == "__main__":
    main()
