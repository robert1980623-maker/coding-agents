"""SQLite storage backend implementation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import structlog

from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    Session,
    SessionStatus,
)

logger = structlog.get_logger(__name__)

# Convert SessionStatus to string for SQL
_STATUS_VALUES = {s: s.value for s in SessionStatus}
_EVENT_TYPE_VALUES = {e: e.value for e in EventType}
_AGENT_TYPE_VALUES = {a: a.value for a in AgentType}


def _dt_to_ts(dt: Optional[datetime]) -> Optional[float]:
    """Convert datetime to Unix timestamp for SQLite storage."""
    if dt is None:
        return None
    return dt.timestamp()


def _ts_to_dt(ts: Optional[float]) -> Optional[datetime]:
    """Convert Unix timestamp from SQLite to datetime."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _dict_to_json(d: dict[str, Any]) -> str:
    """Serialize dict to JSON string for storage."""
    return json.dumps(d, ensure_ascii=False)


def _json_to_dict(s: Optional[str]) -> dict[str, Any]:
    """Deserialize JSON string from storage to dict."""
    if s is None:
        return {}
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


class SQLiteStorage:
    """SQLite-based storage backend.

    Uses asyncio.to_thread to avoid blocking the event loop on I/O.
    """

    def __init__(self, db_path: str | Path = "~/.coding-agents/data.db"):
        self._db_path = Path(db_path).expanduser().resolve()
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def _get_conn(self) -> sqlite3.Connection:
        """Lazily open a connection with WAL mode and PRAGMAs."""
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await asyncio.to_thread(self._open_conn)
        return self._conn

    def _open_conn(self) -> sqlite3.Connection:
        # check_same_thread=False is required because asyncio.to_thread may run
        # on different threads from the pool across calls.
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    async def initialize(self) -> None:
        """Create tables, indexes, triggers, and FTS5 virtual table."""
        conn = await self._get_conn()
        await asyncio.to_thread(self._create_schema, conn)

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ---- Session operations ----

    async def create_session(self, session: Session) -> str:
        conn = await self._get_conn()
        now_ts = datetime.now(timezone.utc).timestamp()
        # Hold self._lock across both execute and commit so concurrent
        # parallel callers (e.g. multiple run_flow tasks gathered together)
        # cannot interleave their writes on the shared sqlite3.Connection.
        # Without this, asyncio.to_thread can dispatch two INSERTs onto
        # different threads simultaneously, triggering "bad parameter or
        # other API misuse" / "Recursive use of cursors not allowed" from
        # SQLite. All other write methods (update_session, add_tag,
        # create_event, …) already take this lock; this method was the
        # outlier and the source of flakiness in tests with parallel
        # task creation (e.g. test_complex_dag_topological_order).
        async with self._lock:
            await asyncio.to_thread(
                conn.execute,
                """
                INSERT INTO sessions (id, agent, prompt, workdir, status, pid, exit_code,
                    started_at, finished_at, duration_ms, last_heartbeat_at,
                    cost_usd, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                    model, provider, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.agent.value if isinstance(session.agent, AgentType) else session.agent,
                    session.prompt,
                    session.workdir,
                    session.status.value if isinstance(session.status, SessionStatus) else session.status,
                    session.pid,
                    session.exit_code,
                    _dt_to_ts(session.started_at),
                    _dt_to_ts(session.finished_at),
                    session.duration_ms,
                    _dt_to_ts(session.last_heartbeat_at),
                    session.cost_usd,
                    session.input_tokens,
                    session.output_tokens,
                    session.cache_read_tokens,
                    session.cache_write_tokens,
                    session.model,
                    session.provider,
                    _dict_to_json(session.metadata),
                    _dt_to_ts(session.created_at) or now_ts,
                    _dt_to_ts(session.updated_at) or now_ts,
                ),
            )
            await asyncio.to_thread(conn.commit)
        return session.id

    async def get_session(self, session_id: str) -> Optional[Session]:
        conn = await self._get_conn()
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if row is None:
            return None
        return self._row_to_session(row)

    async def update_session(self, session_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        conn = await self._get_conn()
        # Convert enum values and datetimes
        processed: dict[str, Any] = {}
        for key, value in kwargs.items():
            if isinstance(value, SessionStatus):
                processed[key] = value.value
            elif isinstance(value, AgentType):
                processed[key] = value.value
            elif isinstance(value, EventType):
                processed[key] = value.value
            elif isinstance(value, datetime):
                processed[key] = _dt_to_ts(value)
            elif key == "metadata" and isinstance(value, dict):
                processed[key] = _dict_to_json(value)
            else:
                processed[key] = value

        processed["updated_at"] = datetime.now(timezone.utc).timestamp()

        set_clause = ", ".join(f"{k} = ?" for k in processed.keys())
        values = list(processed.values()) + [session_id]

        async with self._lock:
            await asyncio.to_thread(
                self._update_session_and_commit,
                conn,
                f"UPDATE sessions SET {set_clause} WHERE id = ?",  # nosec B608
                values,
            )

    def _update_session_and_commit(
        self, conn: sqlite3.Connection, sql: str, values: list
    ) -> None:
        """Execute update and commit in a single thread call to reduce overhead."""
        conn.execute(sql, values)
        conn.commit()

    async def list_sessions(
        self,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        tags: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[Session]:
        conn = await self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if agent is not None:
            conditions.append("s.agent = ?")
            params.append(agent)
        if status is not None:
            conditions.append("s.status = ?")
            params.append(status)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        if tags:
            # Build subquery: sessions with ALL specified tags
            tag_placeholders = ", ".join("?" for _ in tags)
            params.extend(tags)
            tag_count = len(tags)
            sql = (
                "SELECT s.* FROM sessions s "
                "INNER JOIN ( "
                "SELECT session_id FROM session_tags "
                f"WHERE tag IN ({tag_placeholders}) "  # nosec B608
                "GROUP BY session_id HAVING COUNT(DISTINCT tag) = ? "
                f") st ON st.session_id = s.id {where} "  # nosec B608
                "ORDER BY s.created_at DESC LIMIT ?"
            )
            params.append(tag_count)
            params.append(limit)
            cursor = await asyncio.to_thread(conn.execute, sql, params)
        else:
            sql = f"SELECT * FROM sessions s {where} ORDER BY s.created_at DESC LIMIT ?"  # nosec B608
            params.append(limit)
            cursor = await asyncio.to_thread(conn.execute, sql, params)

        rows = await asyncio.to_thread(cursor.fetchall)
        return [self._row_to_session(r) for r in rows]

    async def poll_summary(
        self,
        statuses: Optional[list[str]] = None,
        limit: int = 20,
    ) -> list[tuple[Session, Optional[dict[str, Any]]]]:
        """Return sessions + their latest event in a single query.

        Used by ``coding-agents poll`` to avoid N+1 event lookups. Each
        tuple is ``(session, last_event_info)`` where *last_event_info*
        is ``None`` when the session has no events, or a dict with keys
        ``type`` (str), ``seq`` (int), ``data_bytes`` (int) when present.

        Args:
            statuses: whitelist of status values (e.g. ``['running', 'pending']``).
                ``None`` means no status filter.
            limit: max sessions to return, ordered by ``created_at DESC``.
        """
        conn = await self._get_conn()
        conditions: list[str] = []
        params: list[Any] = []

        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            conditions.append(f"s.status IN ({placeholders})")  # nosec B608
            params.extend(statuses)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        # LEFT JOIN with the latest event per session (by max seq).
        # s.* is unambiguous because we prefix with the sessions alias;
        # the three event columns are explicitly namespaced with e.*.
        sql = f"""
            SELECT s.*, e.type AS ev_type, e.seq AS ev_seq,
                   length(e.data) AS ev_data_bytes
            FROM sessions s
            LEFT JOIN (
                SELECT session_id, type, seq, data
                FROM events
                WHERE (session_id, seq) IN (
                    SELECT session_id, MAX(seq)
                    FROM events
                    GROUP BY session_id
                )
            ) e ON e.session_id = s.id
            {where}
            ORDER BY s.created_at DESC
            LIMIT ?
        """  # nosec B608
        params.append(limit)

        cursor = await asyncio.to_thread(conn.execute, sql, params)
        rows = await asyncio.to_thread(cursor.fetchall)

        results: list[tuple[Session, Optional[dict[str, Any]]]] = []
        for row in rows:
            session = self._row_to_session(row)
            d = dict(row)
            ev_type = d.get("ev_type")
            if ev_type is not None:
                last_event = {
                    "type": ev_type,
                    "seq": d.get("ev_seq", 0),
                    "data_bytes": d.get("ev_data_bytes") or 0,
                }
            else:
                last_event = None
            results.append((session, last_event))
        return results

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        d = dict(row)
        agent_str = d["agent"]
        status_str = d["status"]
        return Session(
            id=d["id"],
            agent=AgentType(agent_str),
            prompt=d["prompt"],
            workdir=d["workdir"],
            status=SessionStatus(status_str),
            pid=d.get("pid"),
            exit_code=d.get("exit_code"),
            started_at=_ts_to_dt(d.get("started_at")),
            finished_at=_ts_to_dt(d.get("finished_at")),
            duration_ms=d.get("duration_ms"),
            last_heartbeat_at=_ts_to_dt(d.get("last_heartbeat_at")),
            cost_usd=d.get("cost_usd"),
            input_tokens=d.get("input_tokens"),
            output_tokens=d.get("output_tokens"),
            cache_read_tokens=d.get("cache_read_tokens"),
            cache_write_tokens=d.get("cache_write_tokens"),
            model=d.get("model"),
            provider=d.get("provider"),
            metadata=_json_to_dict(d.get("metadata")),
            created_at=_ts_to_dt(d.get("created_at")) or datetime.now(timezone.utc),
            updated_at=_ts_to_dt(d.get("updated_at")) or datetime.now(timezone.utc),
        )

    # ---- Tag operations ----

    async def add_tag(self, session_id: str, tag: str) -> None:
        conn = await self._get_conn()
        async with self._lock:
            try:
                await asyncio.to_thread(
                    conn.execute,
                    "INSERT INTO session_tags (session_id, tag) VALUES (?, ?)",
                    (session_id, tag),
                )
                await asyncio.to_thread(conn.commit)
            except sqlite3.IntegrityError:
                # Tag already exists — idempotent
                pass

    async def remove_tag(self, session_id: str, tag: str) -> None:
        conn = await self._get_conn()
        async with self._lock:
            await asyncio.to_thread(
                conn.execute,
                "DELETE FROM session_tags WHERE session_id = ? AND tag = ?",
                (session_id, tag),
            )
            await asyncio.to_thread(conn.commit)

    async def list_tags(self, session_id: str) -> list[str]:
        conn = await self._get_conn()
        cursor = await asyncio.to_thread(
            conn.execute,
            "SELECT tag FROM session_tags WHERE session_id = ? ORDER BY tag",
            (session_id,),
        )
        rows = await asyncio.to_thread(cursor.fetchall)
        return [r["tag"] for r in rows]

    # ---- Event operations ----

    async def append_events(self, events: list[Event]) -> None:
        if not events:
            return
        conn = await self._get_conn()
        rows = [
            (
                e.session_id,
                e.channel,
                e.seq,
                e.type.value if isinstance(e.type, EventType) else e.type,
                e.data,
                e.raw_json,
                _dict_to_json(e.metadata),
                _dt_to_ts(e.created_at) or datetime.now(timezone.utc).timestamp(),
            )
            for e in events
        ]
        async with self._lock:
            await asyncio.to_thread(
                self._insert_events_and_commit,
                conn,
                rows,
            )

    def _insert_events_and_commit(
        self, conn: sqlite3.Connection, rows: list[tuple]
    ) -> None:
        """Insert events and commit in a single thread call to reduce overhead."""
        conn.executemany(
            """
            INSERT INTO events (session_id, channel, seq, type, data, raw_json, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    async def get_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> list[Event]:
        conn = await self._get_conn()
        sql = "SELECT * FROM events WHERE session_id = ? AND seq > ? ORDER BY seq"
        params: list[Any] = [session_id, after_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = await asyncio.to_thread(conn.execute, sql, params)
        rows = await asyncio.to_thread(cursor.fetchall)
        return [self._row_to_event(r) for r in rows]

    async def get_latest_events(
        self,
        session_id: str,
        limit: int = 20,
    ) -> list[Event]:
        """Return the last N events for a session, oldest-first.

        Bounded output designed for the `status` / `tail` commands so they
        fit inside the OpenClaw exec 1MB buffer.
        """
        conn = await self._get_conn()
        # Fetch newest N via subquery, then re-sort ascending so the
        # caller sees events in natural reading order.
        sql = (
            "SELECT * FROM ("
            "  SELECT * FROM events WHERE session_id = ?"
            "  ORDER BY seq DESC LIMIT ?"
            ") ORDER BY seq ASC"
        )
        cursor = await asyncio.to_thread(
            conn.execute, sql, [session_id, limit]
        )
        rows = await asyncio.to_thread(cursor.fetchall)
        return [self._row_to_event(r) for r in rows]

    async def stream_events(
        self,
        session_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """Stream events for a session, polling for new events in real-time.

        For running sessions, this continuously polls for new events every second
        until the session reaches a terminal status (COMPLETED/FAILED/KILLED/TIMEOUT/ORPHANED)
        or the 30-minute timeout is reached.

        Args:
            session_id: The session to stream events for.
            after_seq: Only return events with seq > this value.
        """
        current_seq = after_seq
        poll_interval = 1.0  # seconds between polls — exponential backoff
        max_total_wait = 30 * 60  # 30 minutes
        elapsed = 0.0

        while elapsed < max_total_wait:
            # Fetch new events since last seen seq
            events = await self.get_events(session_id, after_seq=current_seq)
            for event in events:
                yield event
                if event.seq > current_seq:
                    current_seq = event.seq

            # Reset to high-frequency polling when new events arrive
            if events:
                poll_interval = 1.0

            # Check session status — stop if terminal
            session = await self.get_session(session_id)
            if session is None or session.status.is_terminal:
                return

            # Sleep before next poll
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            # Exponential backoff — cap at 10 s
            poll_interval = min(poll_interval * 2, 10.0)

    async def search_events(
        self,
        query: str,
        agent: Optional[str] = None,
        limit: int = 20,
    ) -> list[Event]:
        conn = await self._get_conn()
        # Escape FTS5 query: wrap in double quotes for exact phrase match
        safe_query = '"' + query.replace('"', '""') + '"'
        if agent:
            sql = """
                SELECT e.* FROM events e
                INNER JOIN events_fts f ON f.rowid = e.id
                INNER JOIN sessions s ON s.id = e.session_id
                WHERE events_fts MATCH ? AND s.agent = ?
                ORDER BY e.created_at DESC
                LIMIT ?
            """
            params: list[Any] = [safe_query, agent, limit]
        else:
            sql = """
                SELECT e.* FROM events e
                INNER JOIN events_fts f ON f.rowid = e.id
                WHERE events_fts MATCH ?
                ORDER BY e.created_at DESC
                LIMIT ?
            """
            params = [safe_query, limit]

        cursor = await asyncio.to_thread(conn.execute, sql, params)
        rows = await asyncio.to_thread(cursor.fetchall)
        return [self._row_to_event(r) for r in rows]

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        d = dict(row)
        return Event(
            id=d["id"],
            session_id=d["session_id"],
            channel=d["channel"],
            seq=d["seq"],
            type=EventType(d["type"]),
            data=d["data"],
            raw_json=d.get("raw_json"),
            metadata=_json_to_dict(d.get("metadata")),
            created_at=_ts_to_dt(d.get("created_at")) or datetime.now(timezone.utc),
        )

    # ---- Recovery ----

    async def recover_orphaned_sessions(self, timeout_seconds: int = 300) -> int:
        conn = await self._get_conn()
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = now_ts - timeout_seconds
        async with self._lock:
            cursor = await asyncio.to_thread(
                conn.execute,
                """
                UPDATE sessions
                SET status = 'orphaned', exit_code = -1,
                    finished_at = ?, updated_at = ?
                WHERE status = 'running'
                  AND (last_heartbeat_at IS NULL OR last_heartbeat_at < ?)
                """,
                (now_ts, now_ts, cutoff),
            )
            await asyncio.to_thread(conn.commit)
            return cursor.rowcount

    async def delete_session(self, session_id: str) -> None:
        """Delete a session and all its events (and tags)."""
        conn = await self._get_conn()
        async with self._lock:
            await asyncio.to_thread(
                conn.execute,
                "DELETE FROM session_tags WHERE session_id = ?",
                (session_id,),
            )
            await asyncio.to_thread(
                conn.execute,
                "DELETE FROM events WHERE session_id = ?",
                (session_id,),
            )
            await asyncio.to_thread(
                conn.execute,
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )
            await asyncio.to_thread(conn.commit)

    async def prune_events_keep_result(self, session_id: str) -> int:
        """Drop all events for a session EXCEPT its result event.

        Returns the number of events deleted. If the session has no
        result event, all events are deleted.
        """
        conn = await self._get_conn()
        async with self._lock:
            cursor = await asyncio.to_thread(
                conn.execute,
                """
                DELETE FROM events
                WHERE session_id = ?
                  AND NOT (channel = 'system' AND type = 'result')
                """,
                (session_id,),
            )
            await asyncio.to_thread(conn.commit)
            return cursor.rowcount

    async def vacuum(self) -> None:
        """Run SQLite VACUUM to reclaim disk space after deletes."""
        conn = await self._get_conn()
        # VACUUM cannot run inside a transaction.
        async with self._lock:
            await asyncio.to_thread(conn.execute, "VACUUM")


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA mmap_size=268435456;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    prompt TEXT NOT NULL,
    workdir TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',

    pid INTEGER,
    exit_code INTEGER,

    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    last_heartbeat_at REAL,

    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,

    model TEXT,
    provider TEXT,
    metadata TEXT DEFAULT '{}',

    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),
    updated_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);

CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    channel TEXT NOT NULL DEFAULT 'stdout',
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    raw_json TEXT,
    metadata TEXT DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),

    UNIQUE(session_id, seq)
);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    data, content=events, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, data) VALUES (new.id, new.data);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, data) VALUES('delete', old.id, old.data);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, data) VALUES('delete', old.id, old.data);
    INSERT INTO events_fts(rowid, data) VALUES (new.id, new.data);
END;

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_heartbeat ON sessions(last_heartbeat_at) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""
