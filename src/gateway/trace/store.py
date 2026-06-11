"""TraceStore — SQLite-backed persistence layer for traces and spans.

Uses aiosqlite for async database operations.
Schema v2 — extended per TRACE_REFACTOR_PLAN.md with P0-P3 fields + span_contents table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from shared.logging import get_logger
from shared.models import TokenUsage, TraceSpan

logger = get_logger()

# ============================================================================
# 基础表定义（首次启动时创建）
# ============================================================================
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    agent_id TEXT,
    session_id TEXT,
    client_ip TEXT,
    user_agent TEXT,
    total_tokens INTEGER DEFAULT 0,
    total_latency_ms REAL DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
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
    request_path TEXT DEFAULT '',
    is_stream INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    ttft_ms REAL DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    finish_reason TEXT,
    error_message TEXT,
    temperature REAL,
    max_tokens INTEGER,
    tool_calls_json TEXT,
    content_id TEXT,
    request_summary TEXT,
    response_summary TEXT,
    request_body_json TEXT,
    response_body_json TEXT,
    guard_hits_json TEXT DEFAULT '[]',
    eval_scores_json TEXT DEFAULT '{}',
    upstream_url TEXT,
    gateway_version TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trace_id) REFERENCES traces(trace_id)
);

CREATE TABLE IF NOT EXISTS span_contents (
    content_id TEXT PRIMARY KEY,
    span_id TEXT NOT NULL,
    request_body TEXT,
    response_body TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (span_id) REFERENCES spans(span_id)
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_provider ON spans(provider);
CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status);
CREATE INDEX IF NOT EXISTS idx_spans_created ON spans(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_created ON traces(created_at);
CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent_id);
"""

# ============================================================================
# 增量迁移：ALTER TABLE 添加新列（幂等执行）
# ============================================================================
MIGRATIONS_SQL: list[tuple[str, str, str]] = [
    # ("table", "column", "type_suffix")
    # traces 表新增
    ("traces", "session_id", "TEXT"),
    ("traces", "client_ip", "TEXT"),
    ("traces", "user_agent", "TEXT"),
    ("traces", "estimated_cost_usd", "REAL DEFAULT 0"),
    # spans 表 P0 新增
    ("spans", "finish_reason", "TEXT"),
    ("spans", "error_message", "TEXT"),
    ("spans", "temperature", "REAL"),
    ("spans", "max_tokens", "INTEGER"),
    ("spans", "tool_calls_json", "TEXT"),
    ("spans", "content_id", "TEXT"),
    ("spans", "request_summary", "TEXT"),
    ("spans", "response_summary", "TEXT"),
    ("spans", "request_body_json", "TEXT"),
    ("spans", "response_body_json", "TEXT"),
    # spans 表 P1 新增
    ("spans", "ttft_ms", "REAL DEFAULT 0"),
    ("spans", "estimated_cost_usd", "REAL DEFAULT 0"),
    ("spans", "is_stream", "INTEGER DEFAULT 0"),
    # spans 表 P2 新增
    ("spans", "request_path", "TEXT DEFAULT ''"),
    ("spans", "guard_hits_json", "TEXT DEFAULT '[]'"),
    ("spans", "eval_scores_json", "TEXT DEFAULT '{}'"),
    # spans 表 P3 新增
    ("spans", "upstream_url", "TEXT"),
    ("spans", "gateway_version", "TEXT"),
]


class TraceStore:
    """Async SQLite store for trace and span data — Schema v2.

    Precondition: db_path 的父目录可写。
    Postcondition: initialize() 后所有表和索引就绪。
    """

    db_path: Path

    def __init__(self, db_path: str = "data/gateway.db") -> None:
        """
        Args:
            db_path: SQLite 数据库文件路径，会自动创建父目录
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """打开数据库连接并执行幂等 Schema 迁移。

        Raises:
            aiosqlite.Error — 数据库连接/操作失败
        """
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        # 1. 创建基础表（幂等：IF NOT EXISTS）
        await self._db.executescript(CREATE_TABLES_SQL)
        await self._db.commit()

        # 2. 增量迁移：为已有数据库添加缺失列（幂等）
        await self._run_migrations()

        # 3. 确保 span_contents 表存在（基础表 SQL 已包含，但旧库可能缺失）
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS span_contents ("
            "content_id TEXT PRIMARY KEY,"
            "span_id TEXT NOT NULL,"
            "request_body TEXT,"
            "response_body TEXT,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "FOREIGN KEY (span_id) REFERENCES spans(span_id)"
            ")"
        )

        # 4. 确保新增索引存在
        new_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status)",
            "CREATE INDEX IF NOT EXISTS idx_spans_created ON spans(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(agent_id)",
        ]
        for idx_sql in new_indexes:
            await self._db.execute(idx_sql)

        await self._db.commit()
        logger.info("trace_store_initialized", db_path=str(self.db_path))

    async def _run_migrations(self) -> None:
        """幂等执行 ALTER TABLE ADD COLUMN。

        对于每个目标列，先查询 PRAGMA table_info 确认列不存在再执行。
        """
        for table_name, column_name, type_suffix in MIGRATIONS_SQL:
            async with self._db.execute(
                f"PRAGMA table_info({table_name})"
            ) as cursor:
                existing_columns = {row[1] async for row in cursor}

            if column_name in existing_columns:
                continue  # 列已存在，跳过

            try:
                await self._db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {type_suffix}"
                )
                logger.debug(
                    "migration_added_column",
                    table=table_name,
                    column=column_name,
                )
            except aiosqlite.OperationalError as exc:
                # 极端情况：并发创建导致的重复列
                if "duplicate column" in str(exc).lower():
                    logger.debug(
                        "migration_column_already_exists",
                        table=table_name,
                        column=column_name,
                    )
                else:
                    raise
        await self._db.commit()

    async def close(self) -> None:
        """关闭数据库连接。幂等操作。"""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        """获取数据库连接。

        Raises:
            RuntimeError: 未调用 initialize()
        """
        if self._db is None:
            raise RuntimeError("TraceStore not initialized — call initialize() first")
        return self._db

    # ------------------------------------------------------------------
    # Trace 操作
    # ------------------------------------------------------------------

    async def create_trace(
        self,
        trace_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """创建一条 trace 记录。

        Raises:
            aiosqlite.Error — 写入失败
        """
        await self.db.execute(
            "INSERT OR REPLACE INTO traces "
            "(trace_id, agent_id, session_id, client_ip, user_agent, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'ok', ?)",
            (
                trace_id,
                agent_id,
                session_id,
                client_ip,
                user_agent,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.db.commit()

    async def get_trace(self, trace_id: str) -> dict[str, object] | None:
        """按主键查询 trace。

        Raises:
            aiosqlite.Error
        """
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
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        """分页查询 traces，按 created_at DESC。

        Raises:
            aiosqlite.Error
        """
        conditions: list[str] = []
        params: list[str | int] = []

        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where_clause = ""
        if conditions:
            where_clause = f"WHERE {' AND '.join(conditions)}"

        params.extend([limit, offset])
        async with self.db.execute(
            f"SELECT * FROM traces {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_trace(
        self,
        trace_id: str,
        total_tokens: int | None = None,
        total_latency_ms: float | None = None,
        estimated_cost_usd: float | None = None,
        status: str | None = None,
    ) -> None:
        """更新 trace 聚合统计。仅更新非 None 字段。

        Raises:
            aiosqlite.Error
        """
        updates: list[str] = []
        params: list[str | int | float] = []

        if total_tokens is not None:
            updates.append("total_tokens = ?")
            params.append(total_tokens)
        if total_latency_ms is not None:
            updates.append("total_latency_ms = ?")
            params.append(total_latency_ms)
        if estimated_cost_usd is not None:
            updates.append("estimated_cost_usd = ?")
            params.append(estimated_cost_usd)
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

    # ------------------------------------------------------------------
    # Span 操作
    # ------------------------------------------------------------------

    async def create_span(self, span: TraceSpan) -> None:
        """INSERT OR REPLACE 一条 span 记录。

        Raises:
            aiosqlite.IntegrityError — trace_id 不存在
            aiosqlite.Error — 其他写入失败
        """
        await self.db.execute(
            """INSERT OR REPLACE INTO spans
            (span_id, trace_id, parent_span_id, provider, model,
             request_hash, request_path, is_stream,
             status, prompt_tokens, completion_tokens, latency_ms,
             ttft_ms, estimated_cost_usd,
             finish_reason, error_message, temperature, max_tokens,
             tool_calls_json, content_id,
             request_summary, response_summary,
             request_body_json, response_body_json,
             guard_hits_json, eval_scores_json,
             upstream_url, gateway_version,
             created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                span.span_id,
                span.trace_id,
                span.parent_span_id,
                span.provider,
                span.model,
                span.request_hash,
                span.request_path,
                span.is_stream,
                span.status,
                span.token_usage.prompt_tokens if span.token_usage else 0,
                span.token_usage.completion_tokens if span.token_usage else 0,
                span.latency_ms,
                span.ttft_ms,
                span.estimated_cost_usd,
                span.finish_reason,
                span.error_message,
                span.temperature,
                span.max_tokens,
                span.tool_calls_json,
                span.content_id,
                span.request_summary,
                span.response_summary,
                span.request_body_json,
                span.response_body_json,
                span.guard_hits_json if span.guard_hits_json else "[]",
                span.eval_scores_json if span.eval_scores_json else "{}",
                span.upstream_url,
                span.gateway_version,
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
        ttft_ms: float = 0.0,
        estimated_cost_usd: float = 0.0,
        finish_reason: str | None = None,
        error_message: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_calls_json: str | None = None,
        guard_hits_json: str | None = None,
        eval_scores_json: str | None = None,
        request_summary: str | None = None,
        response_summary: str | None = None,
        content_id: str | None = None,
        request_body_json: str | None = None,
        response_body_json: str | None = None,
        upstream_url: str | None = None,
        gateway_version: str | None = None,
    ) -> None:
        """UPDATE span 写入最终数据。仅更新传入的非 None 参数。

        Raises:
            aiosqlite.Error — 写入失败
        """
        parts: list[str] = ["status = ?"]
        params: list[str | int | float] = [status]

        if token_usage is not None:
            parts.extend(["prompt_tokens = ?", "completion_tokens = ?"])
            params.extend([token_usage.prompt_tokens, token_usage.completion_tokens])

        parts.append("latency_ms = ?")
        params.append(latency_ms)

        if ttft_ms is not None:
            parts.append("ttft_ms = ?")
            params.append(ttft_ms)

        if estimated_cost_usd is not None:
            parts.append("estimated_cost_usd = ?")
            params.append(estimated_cost_usd)

        if finish_reason is not None:
            parts.append("finish_reason = ?")
            params.append(finish_reason)

        if error_message is not None:
            parts.append("error_message = ?")
            params.append(error_message)

        if temperature is not None:
            parts.append("temperature = ?")
            params.append(temperature)

        if max_tokens is not None:
            parts.append("max_tokens = ?")
            params.append(max_tokens)

        if tool_calls_json is not None:
            parts.append("tool_calls_json = ?")
            params.append(tool_calls_json)

        if guard_hits_json is not None:
            parts.append("guard_hits_json = ?")
            params.append(guard_hits_json)

        if eval_scores_json is not None:
            parts.append("eval_scores_json = ?")
            params.append(eval_scores_json)

        if request_summary is not None:
            parts.append("request_summary = ?")
            params.append(request_summary)

        if response_summary is not None:
            parts.append("response_summary = ?")
            params.append(response_summary)

        if content_id is not None:
            parts.append("content_id = ?")
            params.append(content_id)

        if request_body_json is not None:
            parts.append("request_body_json = ?")
            params.append(request_body_json)

        if response_body_json is not None:
            parts.append("response_body_json = ?")
            params.append(response_body_json)

        if upstream_url is not None:
            parts.append("upstream_url = ?")
            params.append(upstream_url)

        if gateway_version is not None:
            parts.append("gateway_version = ?")
            params.append(gateway_version)

        params.append(span_id)
        await self.db.execute(
            f"UPDATE spans SET {', '.join(parts)} WHERE span_id = ?",
            tuple(params),
        )
        await self.db.commit()

    async def get_spans(self, trace_id: str) -> list[dict[str, object]]:
        """获取 trace 下所有 span，按 created_at ASC。

        Raises:
            aiosqlite.Error
        """
        async with self.db.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY created_at ASC",
            (trace_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_span(self, span_id: str) -> dict[str, object] | None:
        """按主键查询单条 span。

        Raises:
            aiosqlite.Error
        """
        async with self.db.execute(
            "SELECT * FROM spans WHERE span_id = ?", (span_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # SpanContent 操作（新增）
    # ------------------------------------------------------------------

    async def insert_span_content(
        self,
        content_id: str,
        span_id: str,
        request_body: str,
        response_body: str,
    ) -> None:
        """INSERT 一条大体积内容记录到 span_contents 表。

        Raises:
            aiosqlite.IntegrityError — 主键冲突或 span_id 不存在
            aiosqlite.Error — 其他写入失败
        """
        await self.db.execute(
            "INSERT INTO span_contents (content_id, span_id, request_body, response_body, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                content_id,
                span_id,
                request_body,
                response_body,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.db.commit()

    async def get_span_content(
        self, content_id: str
    ) -> dict[str, object] | None:
        """按主键查询大体积内容。

        Raises:
            aiosqlite.Error
        """
        async with self.db.execute(
            "SELECT * FROM span_contents WHERE content_id = ?",
            (content_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # 聚合统计
    # ------------------------------------------------------------------

    async def get_stats(self, hours: int = 24) -> dict[str, object]:
        """获取最近 N 小时聚合统计（含 P50/P95/P99 延迟等扩展指标）。

        Precondition: hours ∈ [1, 720].
        Raises:
            aiosqlite.Error
        """
        since = datetime.now(timezone.utc)
        since_iso = since.isoformat()

        async with self.db.execute(
            """SELECT
                COUNT(*) as total_requests,
                SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) as blocked_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                AVG(latency_ms) as avg_latency_ms,
                AVG(ttft_ms) as avg_ttft_ms,
                SUM(estimated_cost_usd) as total_estimated_cost_usd
            FROM spans WHERE created_at >= ?""",
            (since_iso,),
        ) as cursor:
            base = await cursor.fetchone()
        result: dict[str, object] = dict(base) if base else {}

        # --- P50 / P95 / P99 延迟分布（应用层计算） ---
        latencies = await self._query_latency_list(since_iso)
        if latencies:
            latencies.sort()
            result["p50_latency_ms"] = latencies[len(latencies) // 2]
            result["p95_latency_ms"] = latencies[int(len(latencies) * 0.95)]
            result["p99_latency_ms"] = latencies[int(len(latencies) * 0.99)]
        else:
            result["p50_latency_ms"] = 0.0
            result["p95_latency_ms"] = 0.0
            result["p99_latency_ms"] = 0.0

        return result

    # ------------------------------------------------------------------
    # 服务质量聚合查询 (Quality Metrics)
    # ------------------------------------------------------------------

    async def get_service_quality_stats(self, hours: int = 24) -> dict[str, object]:
        """聚合服务质量指标：TTFT/TPS/空响应率/流式终端率/重复率趋势。

        Precondition: hours ∈ [1, 720].

        Returns:
            {
                "ttft": {"p50": float, "p95": float, "p99": float},
                "tps":  {"p50": float, "p95": float, "p99": float},
                "empty_response_rate": float,
                "stream_abort_rate": float,
                "repetition": {"avg_score": float, "low_quality_ratio": float},
                "total_spans": int,
            }
        """
        since_iso = datetime.now(timezone.utc).isoformat()

        # ── TTFT 分布 ──
        async with self.db.execute(
            "SELECT ttft_ms FROM spans WHERE created_at >= ? AND ttft_ms > 0",
            (since_iso,),
        ) as cursor:
            ttft_rows = await cursor.fetchall()
        ttft_values = sorted(float(r[0]) for r in ttft_rows)
        ttft = self._calc_percentiles(ttft_values)

        # ── TPS 分布 ──
        async with self.db.execute(
            "SELECT completion_tokens, latency_ms FROM spans "
            "WHERE created_at >= ? AND completion_tokens > 0 AND latency_ms > 0",
            (since_iso,),
        ) as cursor:
            tps_rows = await cursor.fetchall()
        tps_values = sorted(
            float(r[0]) / (float(r[1]) / 1000.0) for r in tps_rows if float(r[1]) > 0
        )
        tps = self._calc_percentiles(tps_values)

        # ── 空响应率 ──
        async with self.db.execute(
            "SELECT "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN completion_tokens = 0 AND finish_reason IS NOT NULL THEN 1 ELSE 0 END) as empty_count "
            "FROM spans WHERE created_at >= ? AND finish_reason IS NOT NULL",
            (since_iso,),
        ) as cursor:
            empty_row = await cursor.fetchone()
        total_finished = int(empty_row[0]) if empty_row and empty_row[0] else 0  # type: ignore[index]
        empty_count = int(empty_row[1]) if empty_row and empty_row[1] else 0  # type: ignore[index]
        empty_response_rate = empty_count / total_finished if total_finished > 0 else 0.0

        # ── 流式终端率 ──
        async with self.db.execute(
            "SELECT "
            "  COUNT(*) as total_stream, "
            "  SUM(CASE WHEN finish_reason != 'stop' THEN 1 ELSE 0 END) as abort_count "
            "FROM spans WHERE created_at >= ? AND is_stream = 1 AND finish_reason IS NOT NULL",
            (since_iso,),
        ) as cursor:
            stream_row = await cursor.fetchone()
        total_stream = int(stream_row[0]) if stream_row and stream_row[0] else 0  # type: ignore[index]
        abort_count = int(stream_row[1]) if stream_row and stream_row[1] else 0  # type: ignore[index]
        stream_abort_rate = abort_count / total_stream if total_stream > 0 else 0.0

        # ── 重复率趋势 ──
        async with self.db.execute(
            "SELECT eval_scores_json FROM spans WHERE created_at >= ? AND eval_scores_json != '{}' AND eval_scores_json != '[]'",
            (since_iso,),
        ) as cursor:
            eval_rows = await cursor.fetchall()
        rep_scores: list[float] = []
        for row in eval_rows:
            try:
                data = json.loads(str(row[0]))
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("name") in ("repetition", "repetition_score"):
                            rep_scores.append(float(item.get("score", 0)))
                elif isinstance(data, dict) and "repetition" in data:
                    rep_scores.append(float(data["repetition"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        avg_repetition = sum(rep_scores) / len(rep_scores) if rep_scores else 1.0
        low_repetition = sum(1 for s in rep_scores if s < 0.5) / len(rep_scores) if rep_scores else 0.0

        # ── 总 span 数 ──
        async with self.db.execute(
            "SELECT COUNT(*) FROM spans WHERE created_at >= ?",
            (since_iso,),
        ) as cursor:
            count_row = await cursor.fetchone()
        total_spans = int(count_row[0]) if count_row else 0  # type: ignore[index]

        return {
            "ttft": ttft,
            "tps": tps,
            "empty_response_rate": empty_response_rate,
            "stream_abort_rate": stream_abort_rate,
            "repetition": {
                "avg_score": round(avg_repetition, 4),
                "low_quality_ratio": round(low_repetition, 4),
            },
            "total_spans": total_spans,
        }

    @staticmethod
    def _calc_percentiles(values: list[float]) -> dict[str, float]:
        """计算 P50/P95/P99."""
        n = len(values)
        if n == 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "p50": values[n // 2],
            "p95": values[int(n * 0.95)],
            "p99": values[int(n * 0.99)],
        }

    # ------------------------------------------------------------------
    # 采样导出
    # ------------------------------------------------------------------

    async def sample_spans(
        self,
        hours: int = 24,
        limit: int = 50,
        filters: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """按条件采样 span 记录，关联加载 span_contents。

        Args:
            hours: 时间窗口（小时）
            limit: 最大返回数量
            filters: 可选过滤条件。
                - model: str — 按模型筛选
                - status: str — 按状态筛选
                - min_latency_ms: float — 最低延迟
                - has_guard_hits: bool — 是否有安全命中
                - has_low_eval: bool — 是否有低分 eval

        Returns:
            span 记录列表（含关联的 request_body/response_body）。
        """
        since_iso = datetime.now(timezone.utc).isoformat()
        conditions = ["s.created_at >= ?"]
        params: list[object] = [since_iso]

        if filters:
            if "model" in filters and filters["model"]:
                conditions.append("s.model = ?")
                params.append(filters["model"])
            if "status" in filters and filters["status"]:
                conditions.append("s.status = ?")
                params.append(filters["status"])
            if "min_latency_ms" in filters and filters["min_latency_ms"]:
                conditions.append("s.latency_ms >= ?")
                params.append(float(cast(float, filters["min_latency_ms"])))
            if filters.get("has_guard_hits"):
                conditions.append(
                    "(s.guard_hits_json IS NOT NULL AND s.guard_hits_json != '[]')"
                )
            if filters.get("has_low_eval"):
                conditions.append(
                    "(s.eval_scores_json IS NOT NULL AND s.eval_scores_json != '{}' AND s.eval_scores_json != '[]')"
                )

        where_clause = " AND ".join(conditions)
        params.append(limit)

        async with self.db.execute(
            f"SELECT s.* FROM spans s WHERE {where_clause} ORDER BY s.created_at DESC LIMIT ?",
            tuple(params),
        ) as cursor:
            rows = await cursor.fetchall()

        results = [dict(r) for r in rows]

        # 关联加载 span_contents
        content_ids = [
            cid for r in results
            if (cid := r.get("content_id")) and isinstance(cid, str) and cid
        ]
        if content_ids:
            content_map: dict[str, str] = {}
            for cid in content_ids:
                content = await self.get_span_content(cid)
                if content:
                    content_map[cid] = content
            for r in results:
                cid = r.get("content_id")
                if cid and cid in content_map:
                    content = cast(dict[str, object], content_map[cid])
                    r["request_body"] = content.get("request_body")
                    r["response_body"] = content.get("response_body")

        return results

    async def _query_latency_list(self, since_iso: str) -> list[float]:
        """查询时间窗口内所有 span 的延迟数据（用于百分位计算）。"""
        async with self.db.execute(
            "SELECT latency_ms FROM spans WHERE created_at >= ? AND latency_ms > 0",
            (since_iso,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [float(r[0]) for r in rows]
