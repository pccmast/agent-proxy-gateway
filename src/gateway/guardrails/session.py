"""SessionStore — 会话级安全状态存储 (v2).

支撑行为安全层的跨请求攻击模式追踪。
使用内存 dict + TTL 驱逐 + LRU 裁剪。
"""

import threading
from datetime import UTC, datetime

from .config import SessionState

# 默认 30 分钟无活动则过期
DEFAULT_SESSION_TTL_SECONDS: int = 30 * 60
# 最大活跃 session 数
MAX_ACTIVE_SESSIONS: int = 10000
# LRU 驱逐比例（超限时驱逐最久未活动的比例）
LRU_EVICT_RATIO: float = 0.2


class SessionStore:
    """会话级安全状态存储 — 内存 dict + TTL 驱逐。

    线程安全（threading.Lock 保护写操作）。

    Precondition: session_id 为非空字符串。
    Postcondition: get_or_create() 始终返回有效的 SessionState。
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = MAX_ACTIVE_SESSIONS,
    ) -> None:
        """
        Args:
            ttl_seconds: 会话超时（秒），默认 1800（30 分钟）
            max_sessions: 最大活跃 session 数，超限时 LRU 驱逐
        """
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def get_or_create(self, session_id: str) -> SessionState:
        """获取已有 session 或创建新 session。

        Postcondition: 返回的 SessionState.last_activity 已更新为当前时间。
        """
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                state = SessionState(session_id=session_id)
                self._sessions[session_id] = state
                self._evict_capacity()
            else:
                state.last_activity = datetime.now(UTC)
            return state

    def get(self, session_id: str) -> SessionState | None:
        """获取已有 session，不存在时返回 None。

        不创建新 session，不更新 last_activity。
        """
        return self._sessions.get(session_id)

    def save(self, state: SessionState) -> None:
        """持久化已 mutate 的 session。

        内存版中 get_or_create 返回的是同一缓存对象，mutate 即生效，
        故此方法为 no-op；SQLiteSessionStore 则会真正写回数据库。
        两类存储统一暴露此方法，便于 GuardrailsEngine 在行为规则
        跑完后统一调用。
        """
        return None  # noqa: RET502

    def reset(self, session_id: str) -> None:
        """重置指定 session（如检测到攻击后清空状态）。

        Postcondition: 该 session_id 条目从存储中移除。
        """
        with self._lock:
            self._sessions.pop(session_id, None)

    def evict_expired(self) -> int:
        """驱逐所有过期 session。

        Postcondition: 返回被驱逐的 session 数量。
        """
        now = datetime.now(UTC)
        expired_ids: list[str] = []
        with self._lock:
            for sid, state in self._sessions.items():
                age = (now - state.last_activity).total_seconds()
                if age > self._ttl_seconds:
                    expired_ids.append(sid)
            for sid in expired_ids:
                del self._sessions[sid]
        return len(expired_ids)

    @property
    def active_count(self) -> int:
        """当前活跃 session 数量。"""
        return len(self._sessions)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _evict_capacity(self) -> None:
        """容量裁剪：活跃 session 数超过 max_sessions 时，按 last_activity 升序驱逐最旧的 20%。

        注意这不是经典 O(1) 双向链表 LRU——仅按 last_activity 时间近似。
        """
        if len(self._sessions) <= self._max_sessions:
            return
        evict_count = max(1, int(self._max_sessions * LRU_EVICT_RATIO))
        # 按 last_activity 升序，驱逐最旧的 evict_count 个
        sorted_sessions = sorted(
            self._sessions.items(),
            key=lambda kv: kv[1].last_activity,
        )
        for sid, _state in sorted_sessions[:evict_count]:
            del self._sessions[sid]
