"""AuditLogger — 安全事件审计记录器 (v2).

复用 TraceStore 的 aiosqlite 连接，写入 audit_events 表。
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .config import AuditEvent

if TYPE_CHECKING:
    from ..trace.store import TraceStore


class AuditLogger:
    """安全事件审计记录器。

    复用 TraceStore 的数据库连接。
    如果 store 为 None，则只做内存记录（降级模式）。
    """

    def __init__(self, store: "TraceStore | None" = None) -> None:
        """
        Args:
            store: TraceStore 实例（可选），如不传则仅内存记录。
        """
        self._store = store
        self._memory_log: list[AuditEvent] = []

    async def log_event(self, event: AuditEvent) -> None:
        """记录一次安全事件。

        优先写入数据库，失败时降级到内存。
        """
        self._memory_log.append(event)
        if self._store is not None:
            try:
                await self._store.db.execute(
                    """INSERT INTO audit_events
                    (event_id, event_type, rule_id, session_id, trace_id, severity, details, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.event_type,
                        event.rule_id,
                        event.session_id,
                        event.trace_id,
                        event.severity,
                        event.details,
                        event.created_at.isoformat(),
                    ),
                )
                await self._store.db.commit()
            except Exception:
                # 降级：数据库写入失败时仅保留内存记录
                pass

    async def query(
        self,
        rule_id: str | None = None,
        event_type: str | None = None,
        hours: int = 24,
    ) -> list[AuditEvent]:
        """查询审计事件。

        Args:
            rule_id: 可选，按规则 ID 过滤
            event_type: 可选，按事件类型过滤
            hours: 最近 N 小时
        """
        # 优先从数据库查询
        if self._store is not None:
            try:
                from datetime import timedelta
                since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
                conditions = ["created_at >= ?"]
                params: list[str | int] = [since]
                if rule_id:
                    conditions.append("rule_id = ?")
                    params.append(rule_id)
                if event_type:
                    conditions.append("event_type = ?")
                    params.append(event_type)
                async with self._store.db.execute(
                    f"SELECT * FROM audit_events WHERE {' AND '.join(conditions)} ORDER BY created_at DESC",
                    tuple(params),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        AuditEvent(
                            event_id=str(row["event_id"]),
                            event_type=str(row.get("event_type", "")),
                            rule_id=str(row.get("rule_id", "")),
                            session_id=row.get("session_id"),
                            trace_id=row.get("trace_id"),
                            severity=str(row.get("severity", "medium")),
                            details=str(row.get("details", "")),
                        )
                        for row in rows
                    ]
            except Exception:
                pass
        # 降级到内存查询
        since = datetime.now(timezone.utc)
        from datetime import timedelta
        cutoff = since - timedelta(hours=hours)
        filtered = [
            e for e in self._memory_log
            if e.created_at >= cutoff
            and (not rule_id or e.rule_id == rule_id)
            and (not event_type or e.event_type == event_type)
        ]
        return filtered
