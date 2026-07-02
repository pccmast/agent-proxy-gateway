#!/usr/bin/env python3
"""Generate test data for the Agent Gateway dashboard demo.

Creates realistic-looking trace/span records in the SQLite database
so the Dashboard has data to display immediately.

Usage:
    uv run python scripts/seed_data.py
    uv run python scripts/seed_data.py --count 50  # generate 50 traces
"""

import json
import random
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

DB_PATH = "data/gateway.db"

# Sample data pools
AGENTS = ["agent-alpha", "agent-beta", "agent-gamma", "agent-delta"]
MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo", "claude-3-opus"]
PROVIDERS = ["openai", "openai", "openai", "anthropic"]
STATUSES = ["ok", "ok", "ok", "ok", "ok", "ok", "ok", "blocked", "error", "timeout"]
GUARD_HITS_POOL = [
    [],
    [],
    [],
    ["pii-detection"],
    ["injection-detection"],
    ["content-safety"],
    ["pii-detection", "injection-detection"],
]
EVAL_SCORES_POOL = [
    {},
    {"relevance": 0.95, "safety": 1.0, "coherence": 0.9},
    {"relevance": 0.7, "safety": 0.8, "coherence": 0.65},
    {"relevance": 0.3, "safety": 0.5, "coherence": 0.4},
    {"relevance": 0.99, "safety": 0.95, "coherence": 0.98},
]


def _rand(min_v: float, max_v: float) -> float:
    return round(random.uniform(min_v, max_v), 2)


def _rand_int(min_v: int, max_v: int) -> int:
    return random.randint(min_v, max_v)


def _rand_time(hours_back: int = 24) -> str:
    return (datetime.now(UTC) - timedelta(hours=random.uniform(0, hours_back))).isoformat()


def generate_traces(count: int = 30) -> None:
    """Generate synthetic trace and span records."""
    if not Path(DB_PATH).exists():
        print(f"Database not found at {DB_PATH}. Start the gateway once to create it.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Ensure tables exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            agent_id TEXT,
            total_tokens INTEGER DEFAULT 0,
            total_latency_ms REAL DEFAULT 0,
            status TEXT DEFAULT 'ok',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_span_id TEXT,
            provider TEXT NOT NULL,
            model TEXT,
            request_hash TEXT,
            status TEXT DEFAULT 'ok',
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0,
            guard_hits TEXT,
            eval_scores TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
        )
    """)

    generated = 0
    for i in range(count):
        trace_id = str(uuid.uuid4())
        agent = random.choice(AGENTS)
        status = random.choice(STATUSES)
        n_spans = _rand_int(1, 3)

        # Create trace
        created = _rand_time(72)
        cursor.execute(
            "INSERT INTO traces (trace_id, agent_id, status, created_at) VALUES (?, ?, ?, ?)",
            (trace_id, agent, status, created),
        )

        # Create spans (possibly nested)
        total_tokens = 0
        total_latency = 0.0
        parent_span_id: str | None = None
        root_span_id: str | None = None

        for s in range(n_spans):
            span_id = str(uuid.uuid4())
            if s == 0:
                root_span_id = span_id
                parent_span_id = None
            else:
                parent_span_id = root_span_id

            idx = _rand_int(0, len(MODELS) - 1)
            model = MODELS[idx]
            provider = PROVIDERS[idx]

            prompt_tokens = _rand_int(10, 2000)
            completion_tokens = _rand_int(5, 500)
            latency_ms = _rand(100, 8000)

            guard_hits = json.dumps(random.choice(GUARD_HITS_POOL), ensure_ascii=False)
            eval_scores = json.dumps(random.choice(EVAL_SCORES_POOL), ensure_ascii=False)

            cursor.execute(
                """INSERT INTO spans
                (span_id, trace_id, parent_span_id, provider, model,
                 status, prompt_tokens, completion_tokens, latency_ms,
                 guard_hits, eval_scores, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    span_id,
                    trace_id,
                    parent_span_id,
                    provider,
                    model,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    guard_hits,
                    eval_scores,
                    created,
                ),
            )

            total_tokens += prompt_tokens + completion_tokens
            total_latency += latency_ms

        # Update trace aggregates
        cursor.execute(
            "UPDATE traces SET total_tokens = ?, total_latency_ms = ? WHERE trace_id = ?",
            (total_tokens, round(total_latency, 1), trace_id),
        )

        generated += 1
        if generated % 10 == 0:
            print(f"  Generated {generated}/{count} traces...")

    conn.commit()
    conn.close()

    print(f"\n✅ Generated {generated} traces with {generated * _rand_int(1, 3)} spans (avg)")
    print(f"   Database: {Path(DB_PATH).absolute()}")


if __name__ == "__main__":
    cnt = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].startswith("--count") else 30
    if len(sys.argv) > 2:
        cnt = int(sys.argv[2])
    print(f"Generating {cnt} synthetic traces...")
    generate_traces(cnt)
