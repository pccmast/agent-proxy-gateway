"""SQLiteSessionStore — session state persisted to SQLite via sync sqlite3.

Replaces the in-memory SessionStore so that guardrail behavioural state
(jailbreak escalation scores, tool-call history) survives gateway restarts.

Uses Python's built-in ``sqlite3`` (sync) — no async bridge needed since
the callers (GuardrailsEngine middleware) call session methods from within
the asyncio event loop, where ``run_until_complete`` over aiosqlite would
fail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from shared.logging import get_logger

from .config import SessionState

logger = get_logger()

DEFAULT_SESSION_TTL_SECONDS: int = 30 * 60
MAX_ACTIVE_SESSIONS: int = 10_000

CREATE_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS guard_sessions (
    session_id  TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL DEFAULT '{}',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_guard_sessions_updated
    ON guard_sessions(updated_at);
"""


class SQLiteSessionStore:
    """Session-level security state persisted in SQLite.

    Uses a separate ``sqlite3`` connection (sync) to the same database
    file that TraceStore uses via ``aiosqlite``.  SQLite supports
    concurrent readers/writers, especially with WAL mode.

    Thread-safe for writes via ``threading.Lock``.
    """

    def __init__(
        self,
        db_path: str = "data/gateway.db",
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        max_sessions: int = MAX_ACTIVE_SESSIONS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._db_path = db_path

    # ------------------------------------------------------------------ lifecycle

    def initialize(self) -> None:
        """Open a sync SQLite connection and create the table (call once at startup)."""
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(CREATE_SESSIONS_TABLE)
        self._conn.commit()
        logger.info("sqlite_session_store_initialized", db_path=self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ public

    def get_or_create(self, session_id: str) -> SessionState:
        """Get existing session or create a new one."""
        with self._lock:
            state = self._load(session_id)
            if state is None:
                state = SessionState(session_id=session_id)
                self._save(state)
                self._maybe_evict_lru()
            else:
                state.last_activity = datetime.now(UTC)
                self._save(state)
            return state

    def get(self, session_id: str) -> SessionState | None:
        """Get existing session without creating or updating timestamps."""
        return self._load(session_id)

    def reset(self, session_id: str) -> None:
        """Delete a session (e.g. after detecting an attack pattern)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM guard_sessions WHERE session_id = ?",
                (session_id,),
            )
            self._conn.commit()

    def evict_expired(self) -> int:
        """Delete all sessions older than TTL."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._ttl_seconds)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM guard_sessions WHERE updated_at < ?",
                (cutoff.isoformat(),),
            )
            self._conn.commit()
            return cursor.rowcount

    @property
    def active_count(self) -> int:
        """Current active session count."""
        try:
            row = self._conn.execute("SELECT COUNT(*) FROM guard_sessions").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ---------------------------------------------------------------- internal

    def _load(self, session_id: str) -> SessionState | None:
        """Load a session from SQLite."""
        try:
            row = self._conn.execute(
                "SELECT state_json, updated_at FROM guard_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None

            data = json.loads(row[0])
            updated_at = row[1]

            return SessionState(
                session_id=session_id,
                escalation_score=float(data.get("escalation_score", 0.0)),
                history=data.get("history", []),
                tool_call_history=data.get("tool_call_history", []),
                consecutive_same_tool=int(data.get("consecutive_same_tool", 0)),
                total_tool_calls=int(data.get("total_tool_calls", 0)),
                last_activity=datetime.fromisoformat(data.get("last_activity", updated_at or ""))
                if data.get("last_activity")
                else datetime.now(UTC),
                created_at=datetime.fromisoformat(data.get("created_at", updated_at or ""))
                if data.get("created_at")
                else datetime.now(UTC),
            )
        except Exception:
            return None

    def _save(self, state: SessionState) -> None:
        """Persist a session to SQLite."""
        data = {
            "escalation_score": state.escalation_score,
            "history": state.history,
            "tool_call_history": state.tool_call_history,
            "consecutive_same_tool": state.consecutive_same_tool,
            "total_tool_calls": state.total_tool_calls,
            "created_at": state.created_at.isoformat(),
            "last_activity": state.last_activity.isoformat(),
        }
        state_json = json.dumps(data, ensure_ascii=False)
        now = datetime.now(UTC).isoformat()

        self._conn.execute(
            "INSERT OR REPLACE INTO guard_sessions (session_id, state_json, updated_at) VALUES (?, ?, ?)",
            (state.session_id, state_json, now),
        )
        self._conn.commit()

    def _maybe_evict_lru(self) -> None:
        """Evict oldest sessions if over max_sessions."""
        row = self._conn.execute("SELECT COUNT(*) FROM guard_sessions").fetchone()
        if not row or int(row[0]) <= self._max_sessions:
            return

        evict_count = max(1, int(self._max_sessions * 0.2))
        self._conn.execute(
            "DELETE FROM guard_sessions WHERE session_id IN "
            "(SELECT session_id FROM guard_sessions "
            "ORDER BY updated_at ASC LIMIT ?)",
            (evict_count,),
        )
        self._conn.commit()
