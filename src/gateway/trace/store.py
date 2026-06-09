"""TraceStore — SQLite-backed persistence layer for traces and spans.

Uses aiosqlite for async database operations.
Schema matches PROJECT_SPEC.md Section 3 (Trace Engine).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from shared.logging import get_logger
from shared.models import TokenUsage, TraceSpan

logger = get_logger()

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    agent_id TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_latency_ms REAL DEFAULT 0,
    status TEXT DEFAULT 'ok',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_provider ON spans(provider);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
"""


class TraceStore:
    """Async SQLite store for trace and span data."""

    def __init__(self, db_path: str = "data/gateway.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database and create tables."""
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(CREATE_TABLES_SQL)
        await self._db.commit()
        logger.info("trace_store_initialized", db_path=str(self.db_path))

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("TraceStore not initialized — call initialize() first")
        return self._db

    # --- Trace operations ---

    async def create_trace(
        self,
        trace_id: str,
        agent_id: str | None = None,
    ) -> None:
        """Create a new trace record."""
        await self.db.execute(
            "INSERT OR REPLACE INTO traces (trace_id, agent_id, status, created_at) VALUES (?, ?, 'ok', ?)",
            (trace_id, agent_id, datetime.now(timezone.utc).isoformat()),
        )
        await self.db.commit()

    async def get_trace(self, trace_id: str) -> dict | None:
        """Get a single trace by ID."""
        async with self.db.execute(
            "SELECT * FROM traces WHERE trace_id = ?", (trace_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_traces(
        self,
        limit: int = 50,
        offset: int = 0,
        agent_id: str | None = None,
    ) -> list[dict]:
        """List traces with optional filtering."""
        if agent_id:
            async with self.db.execute(
                "SELECT * FROM traces WHERE agent_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (agent_id, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM traces ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_trace(
        self,
        trace_id: str,
        total_tokens: int | None = None,
        total_latency_ms: float | None = None,
        status: str | None = None,
    ) -> None:
        """Update aggregated trace statistics."""
        updates: list[str] = []
        params: list[str | int | float] = []

        if total_tokens is not None:
            updates.append("total_tokens = ?")
            params.append(total_tokens)
        if total_latency_ms is not None:
            updates.append("total_latency_ms = ?")
            params.append(total_latency_ms)
        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if updates:
            params.append(trace_id)
            await self.db.execute(
                f"UPDATE traces SET {', '.join(updates)} WHERE trace_id = ?",
                tuple(params),
            )
            await self.db.commit()

    # --- Span operations ---

    async def create_span(self, span: TraceSpan) -> None:
        """Insert a new span."""
        await self.db.execute(
            """INSERT OR REPLACE INTO spans
            (span_id, trace_id, parent_span_id, provider, model, request_hash,
             status, prompt_tokens, completion_tokens, latency_ms,
             guard_hits, eval_scores, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                span.span_id,
                span.trace_id,
                span.parent_span_id,
                span.provider,
                span.model,
                span.request_hash,
                span.status,
                span.token_usage.prompt_tokens if span.token_usage else 0,
                span.token_usage.completion_tokens if span.token_usage else 0,
                span.latency_ms,
                json.dumps(span.guard_hits, ensure_ascii=False),
                json.dumps(span.eval_scores, ensure_ascii=False),
                span.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def finish_span(
        self,
        span_id: str,
        status: str = "ok",
        token_usage: TokenUsage | None = None,
        latency_ms: float = 0.0,
        guard_hits: list[str] | None = None,
        eval_scores: dict[str, float] | None = None,
    ) -> None:
        """Update a span with final results."""
        parts: list[str] = ["status = ?"]
        params: list[str | int | float] = [status]

        if token_usage is not None:
            parts.extend([
                "prompt_tokens = ?",
                "completion_tokens = ?",
            ])
            params.extend([token_usage.prompt_tokens, token_usage.completion_tokens])

        parts.append("latency_ms = ?")
        params.append(latency_ms)

        if guard_hits is not None:
            parts.append("guard_hits = ?")
            params.append(json.dumps(guard_hits, ensure_ascii=False))

        if eval_scores is not None:
            parts.append("eval_scores = ?")
            params.append(json.dumps(eval_scores, ensure_ascii=False))

        params.append(span_id)
        await self.db.execute(
            f"UPDATE spans SET {', '.join(parts)} WHERE span_id = ?",
            tuple(params),
        )
        await self.db.commit()

    async def get_spans(self, trace_id: str) -> list[dict]:
        """Get all spans for a trace, ordered by creation time."""
        async with self.db.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY created_at ASC",
            (trace_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_span(self, span_id: str) -> dict | None:
        """Get a single span by ID."""
        async with self.db.execute(
            "SELECT * FROM spans WHERE span_id = ?", (span_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_stats(self, hours: int = 24) -> dict:
        """Get aggregate statistics for the last N hours."""
        since = datetime.now(timezone.utc).isoformat()

        async with self.db.execute(
            """SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                AVG(latency_ms) as avg_latency_ms
            FROM spans WHERE created_at >= ?""",
            (since,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else {}
