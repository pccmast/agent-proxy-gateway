"""One-click startup verification — checks all gateway endpoints are live.

Usage:
    uv run python scripts/startup_check.py              # check local gateway
    uv run python scripts/startup_check.py --proxy       # also test a proxy forward
    uv run python scripts/startup_check.py --json        # machine-readable output
    uv run python scripts/startup_check.py --url http://my-gateway:18080

Exits 0 if all checks pass, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from argparse import ArgumentParser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://localhost:18080"
DEFAULT_TIMEOUT = 10

CHECKS: list[dict[str, object]] = [
    {
        "name": "health",
        "method": "GET",
        "path": "/health",
        "expect_status": 200,
        "expect_body_contains": "ok",
    },
    {
        "name": "metrics",
        "method": "GET",
        "path": "/metrics",
        "expect_status": 200,
        "expect_body_contains": "gateway_requests_total",
    },
    {
        "name": "traces",
        "method": "GET",
        "path": "/api/traces",
        "expect_status": 200,
        "expect_body_contains": "traces",
    },
    {
        "name": "guardrail rules",
        "method": "GET",
        "path": "/api/guardrails/rules",
        "expect_status": 200,
        "expect_body_contains": "rules",
    },
    {
        "name": "trace stats",
        "method": "GET",
        "path": "/api/traces/stats",
        "expect_status": 200,
    },
    {
        "name": "guardrail stats",
        "method": "GET",
        "path": "/api/guardrails/stats",
        "expect_status": 200,
    },
    {
        "name": "budget status",
        "method": "GET",
        "path": "/api/budget/status",
        "expect_status": 200,
    },
    {
        "name": "eval metrics",
        "method": "GET",
        "path": "/api/eval/metrics",
        "expect_status": 200,
    },
]

PROXY_CHECK: dict[str, object] = {
    "name": "proxy forward",
    "method": "POST",
    "path": "/v1/chat/completions",
    "expect_status": [200, 502, 504],  # 200=upstream reachable, 502/504=pathway OK
    "body": '{"model":"deepseek-chat","messages":[{"role":"user","content":"Hi"}],"max_tokens":3}',
}

# ---------------------------------------------------------------------------
# HTTP (stdlib only — no httpx, no requests)
# ---------------------------------------------------------------------------

import urllib.request as _req


def _http_request(method: str, url: str, body: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str]:
    data = body.encode("utf-8") if body else None
    req = _req.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer any-key",
        },
        method=method,
    )
    with _req.urlopen(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
        return resp.status, resp.read().decode("utf-8", errors="replace")[:1024]


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

_COLOUR = {
    "PASS": "\033[32m",
    "FAIL": "\033[31m",
    "SKIP": "\033[33m",
    "RESET": "\033[0m",
    "DIM": "\033[2m",
    "BOLD": "\033[1m",
}


def _c(c: str) -> str:
    return _COLOUR.get(c, "")


class Reporter:
    def __init__(self, *, json_mode: bool = False) -> None:
        self.json_mode = json_mode
        self.results: list[dict[str, object]] = []

    def add(self, r: dict[str, object]) -> None:
        self.results.append(r)

    def done(self, total_s: float) -> int:
        if self.json_mode:
            print(json.dumps({"elapsed_s": round(total_s, 2), "results": self.results}))
        else:
            self._pretty(total_s)
        return 0 if sum(1 for r in self.results if r["status"] == "FAIL") == 0 else 1

    def _pretty(self, total_s: float) -> None:
        print()
        print(f"{_c('BOLD')}─── Results ({total_s:.1f}s) ──{_c('RESET')}")
        for r in self.results:
            s = f"{_c(str(r['status']))}{r['status']:<6}{_c('RESET')}"
            line = f"  {s} {str(r['name']):<28}  {str(r.get('code', '')):<6}  {str(r.get('elapsed_ms', ''))}ms"
            print(line)
            # Print failure detail — shows what actually came back
            detail = str(r.get("detail", ""))
            if detail:
                print(f"  {_c('DIM')}     ↳ {detail}{_c('RESET')}")
            resp_preview = str(r.get("response_preview", ""))
            if resp_preview:
                print(f"  {_c('DIM')}     ↳ body: {resp_preview}{_c('RESET')}")
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        failed = sum(1 for r in self.results if r["status"] == "FAIL")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        print(
            f"\n  {_c('PASS')}{passed} passed{_c('RESET')}  "
            f"{_c('FAIL')}{failed} failed{_c('RESET')}  "
            f"{_c('SKIP')}{skipped} skipped{_c('RESET')}  "
            f"(total {passed + failed + skipped})"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run(base_url: str, check: dict[str, object], reporter: Reporter, timeout: int) -> None:
    name = str(check["name"])
    url = base_url.rstrip("/") + str(check["path"])
    body = check.get("body")

    info: dict[str, object] = {"name": name, "url": url, "status": "SKIP"}
    t0 = time.monotonic()
    try:
        code, resp = _http_request(str(check["method"]), url, str(body) if body else None, timeout)
        info["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        info["code"] = code

        expected = check.get("expect_status")
        acceptable: list[int] = expected if isinstance(expected, list) else [int(expected)]  # type: ignore[arg-type]

        if code not in acceptable:
            info["status"] = "FAIL"
            info["detail"] = f"expected {acceptable}, got {code}"
            info["response_preview"] = resp[:200]
            reporter.add(info)
            return

        expected_text = check.get("expect_body_contains")
        if expected_text and isinstance(expected_text, str) and expected_text not in resp:
            info["status"] = "FAIL"
            info["detail"] = f"body missing '{expected_text}'"
            info["response_preview"] = resp[:200]
            reporter.add(info)
            return

        info["status"] = "PASS"
    except Exception:
        info["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        info["status"] = "FAIL"
        info["detail"] = str(traceback.format_exc()[-200:])
    reporter.add(info)


def main() -> None:
    p = ArgumentParser(description="Startup health-check for Agent Gateway")
    p.add_argument("--url", default=DEFAULT_BASE_URL, help="Gateway base URL")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-request timeout (s)")
    p.add_argument("--proxy", action="store_true", help="Also test a proxy forward")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = p.parse_args()

    reporter = Reporter(json_mode=args.json)
    t0 = time.monotonic()
    for c in CHECKS:
        _run(args.url, c, reporter, args.timeout)
    if args.proxy:
        _run(args.url, PROXY_CHECK, reporter, args.timeout)
    code = reporter.done(time.monotonic() - t0)
    sys.exit(code)


if __name__ == "__main__":
    main()
