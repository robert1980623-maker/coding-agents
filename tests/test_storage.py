"""Tests for SQLiteStorage."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


class TestInitialize:
    async def test_creates_tables(self, storage: SQLiteStorage):
        # Schema already created by fixture; just verify no errors on re-init
        await storage.initialize()

    async def test_creates_db_file(self, tmp_path: Path):
        db_path = tmp_path / "sub" / "test.db"
        store = SQLiteStorage(db_path)
        await store.initialize()
        assert db_path.exists()
        await store.close()


class TestSessionCRUD:
    async def test_create_and_get(self, storage: SQLiteStorage, sample_session: Session):
        sid = await storage.create_session(sample_session)
        assert sid == sample_session.id

        loaded = await storage.get_session(sid)
        assert loaded is not None
        assert loaded.id == sample_session.id
        assert loaded.agent == AgentType.CLAUDE
        assert loaded.prompt == "refactor function"
        assert loaded.status == SessionStatus.PENDING

    async def test_get_nonexistent(self, storage: SQLiteStorage):
        loaded = await storage.get_session("does-not-exist")
        assert loaded is None

    async def test_update_session(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        now = datetime.now(timezone.utc)
        await storage.update_session(
            sample_session.id,
            status=SessionStatus.RUNNING,
            pid=1234,
            started_at=now,
            last_heartbeat_at=now,
        )

        loaded = await storage.get_session(sample_session.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.RUNNING
        assert loaded.pid == 1234
        assert loaded.started_at is not None
        assert loaded.last_heartbeat_at is not None

    async def test_update_metadata(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        await storage.update_session(
            sample_session.id,
            metadata={"key": "value", "count": 42},
        )
        loaded = await storage.get_session(sample_session.id)
        assert loaded is not None
        assert loaded.metadata == {"key": "value", "count": 42}

    async def test_update_noop(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        # Empty kwargs should be a no-op
        await storage.update_session(sample_session.id)
        loaded = await storage.get_session(sample_session.id)
        assert loaded is not None

    async def test_list_sessions(self, storage: SQLiteStorage):
        for i in range(3):
            s = Session(agent=AgentType.CLAUDE, prompt=f"prompt {i}")
            await storage.create_session(s)

        sessions = await storage.list_sessions()
        assert len(sessions) == 3

    async def test_list_sessions_filter_agent(self, storage: SQLiteStorage):
        await storage.create_session(Session(agent=AgentType.CLAUDE, prompt="a"))
        await storage.create_session(Session(agent=AgentType.CODEX, prompt="b"))

        claude_only = await storage.list_sessions(agent="claude")
        assert len(claude_only) == 1
        assert claude_only[0].agent == AgentType.CLAUDE

    async def test_list_sessions_filter_status(self, storage: SQLiteStorage):
        s1 = Session(agent=AgentType.CLAUDE, prompt="a")
        s2 = Session(agent=AgentType.CLAUDE, prompt="b")
        await storage.create_session(s1)
        await storage.create_session(s2)
        await storage.update_session(s2.id, status=SessionStatus.RUNNING)

        running = await storage.list_sessions(status="running")
        assert len(running) == 1
        assert running[0].id == s2.id

    async def test_list_sessions_limit(self, storage: SQLiteStorage):
        for i in range(5):
            await storage.create_session(Session(agent=AgentType.CLAUDE, prompt=str(i)))

        limited = await storage.list_sessions(limit=2)
        assert len(limited) == 2


class TestTags:
    async def test_add_and_list(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        await storage.add_tag(sample_session.id, "important")
        await storage.add_tag(sample_session.id, "urgent")
        tags = await storage.list_tags(sample_session.id)
        assert sorted(tags) == ["important", "urgent"]

    async def test_add_idempotent(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        await storage.add_tag(sample_session.id, "important")
        await storage.add_tag(sample_session.id, "important")
        tags = await storage.list_tags(sample_session.id)
        assert tags == ["important"]

    async def test_remove_tag(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        await storage.add_tag(sample_session.id, "important")
        await storage.remove_tag(sample_session.id, "important")
        tags = await storage.list_tags(sample_session.id)
        assert tags == []

    async def test_remove_nonexistent(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        # Should not raise
        await storage.remove_tag(sample_session.id, "missing")

    async def test_filter_by_tags(self, storage: SQLiteStorage):
        s1 = Session(agent=AgentType.CLAUDE, prompt="a")
        s2 = Session(agent=AgentType.CLAUDE, prompt="b")
        s3 = Session(agent=AgentType.CLAUDE, prompt="c")
        await storage.create_session(s1)
        await storage.create_session(s2)
        await storage.create_session(s3)

        await storage.add_tag(s1.id, "important")
        await storage.add_tag(s1.id, "urgent")
        await storage.add_tag(s2.id, "important")

        # Filter by single tag
        result = await storage.list_sessions(tags=["important"])
        ids = {s.id for s in result}
        assert ids == {s1.id, s2.id}

        # Filter by multiple tags (AND)
        result = await storage.list_sessions(tags=["important", "urgent"])
        ids = {s.id for s in result}
        assert ids == {s1.id}


class TestEvents:
    async def test_append_and_get(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=1, type=EventType.STDOUT, data="line1"),
            Event(session_id=sample_session.id, channel="stdout", seq=2, type=EventType.STDOUT, data="line2"),
        ]
        await storage.append_events(events)

        loaded = await storage.get_events(sample_session.id)
        assert len(loaded) == 2
        assert loaded[0].data == "line1"
        assert loaded[0].seq == 1
        assert loaded[1].data == "line2"

    async def test_get_after_seq(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=i, type=EventType.STDOUT, data=f"line{i}")
            for i in range(1, 6)
        ]
        await storage.append_events(events)

        loaded = await storage.get_events(sample_session.id, after_seq=3)
        assert len(loaded) == 2
        assert [e.seq for e in loaded] == [4, 5]

    async def test_get_with_limit(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=i, type=EventType.STDOUT, data=f"line{i}")
            for i in range(1, 11)
        ]
        await storage.append_events(events)

        loaded = await storage.get_events(sample_session.id, limit=3)
        assert len(loaded) == 3

    async def test_append_empty(self, storage: SQLiteStorage):
        # Should be a no-op
        await storage.append_events([])

    async def test_stream_events(self, storage: SQLiteStorage, sample_session: Session):
        """stream_events should yield all past events then exit when the
        session reaches a terminal status. Without marking the session
        terminal, the streamer would block forever (long-poll design).
        """
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=i, type=EventType.STDOUT, data=f"line{i}")
            for i in range(1, 4)
        ]
        await storage.append_events(events)
        # Mark the session terminal so the long-poll loop exits.
        await storage.update_session(
            sample_session.id, status=SessionStatus.COMPLETED,
        )

        collected = []
        async for e in storage.stream_events(sample_session.id):
            collected.append(e)
        assert len(collected) == 3

    async def test_stream_events_yields_only_new_events_with_after_seq(
        self, storage: SQLiteStorage, sample_session: Session,
    ):
        """stream_events(after_seq=N) must skip events with seq <= N
        and only yield seq > N. Combined with a terminal status, the
        long-poll loop exits after the first poll.
        """
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=i, type=EventType.STDOUT, data=f"line{i}")
            for i in range(1, 6)
        ]
        await storage.append_events(events)
        await storage.update_session(
            sample_session.id, status=SessionStatus.COMPLETED,
        )

        collected = []
        async for e in storage.stream_events(sample_session.id, after_seq=2):
            collected.append(e)
        assert len(collected) == 3
        assert [e.seq for e in collected] == [3, 4, 5]

    async def test_stream_events_empty_session_returns_immediately(
        self, storage: SQLiteStorage, sample_session: Session,
    ):
        """stream_events on a terminal session with no events must return
        without blocking. Long-poll's worst-case behavior would hang.
        """
        await storage.create_session(sample_session)
        await storage.update_session(
            sample_session.id, status=SessionStatus.COMPLETED,
        )
        # Use a hard timeout so a regression that re-enters the long-poll
        # loop fails with a clear error, not a 30-minute hang.
        async def _collect() -> list[Event]:
            return [e async for e in storage.stream_events(sample_session.id)]
        collected = await asyncio.wait_for(_collect(), timeout=5.0)
        assert collected == []


class TestFTS:
    async def test_search_basic(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        events = [
            Event(session_id=sample_session.id, channel="stdout", seq=1, type=EventType.STDOUT, data="refactor the function"),
            Event(session_id=sample_session.id, channel="stdout", seq=2, type=EventType.STDOUT, data="add tests for module"),
            Event(session_id=sample_session.id, channel="stdout", seq=3, type=EventType.STDOUT, data="refactor another piece"),
        ]
        await storage.append_events(events)

        results = await storage.search_events("refactor")
        assert len(results) == 2
        for r in results:
            assert "refactor" in r.data

    async def test_search_with_agent_filter(self, storage: SQLiteStorage):
        s1 = Session(agent=AgentType.CLAUDE, prompt="a")
        s2 = Session(agent=AgentType.CODEX, prompt="b")
        await storage.create_session(s1)
        await storage.create_session(s2)
        await storage.append_events([
            Event(session_id=s1.id, channel="stdout", seq=1, type=EventType.STDOUT, data="refactor"),
            Event(session_id=s2.id, channel="stdout", seq=1, type=EventType.STDOUT, data="refactor"),
        ])

        results = await storage.search_events("refactor", agent="claude")
        assert len(results) == 1
        assert results[0].session_id == s1.id

    async def test_search_no_results(self, storage: SQLiteStorage, sample_session: Session):
        await storage.create_session(sample_session)
        await storage.append_events([
            Event(session_id=sample_session.id, channel="stdout", seq=1, type=EventType.STDOUT, data="hello"),
        ])
        results = await storage.search_events("nonexistent")
        assert results == []


class TestRecovery:
    async def test_recover_orphaned(self, storage: SQLiteStorage):
        # Create a session with very old heartbeat
        s = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(s)
        await storage.update_session(s.id, status=SessionStatus.RUNNING)
        # Manually set old heartbeat via asyncio.to_thread (cross-thread safety)
        old_time = datetime.now(timezone.utc).timestamp() - 600
        conn = await storage._get_conn()
        await asyncio.to_thread(
            conn.execute,
            "UPDATE sessions SET last_heartbeat_at = ? WHERE id = ?",
            (old_time, s.id),
        )
        await asyncio.to_thread(conn.commit)

        count = await storage.recover_orphaned_sessions(timeout_seconds=300)
        assert count == 1

        loaded = await storage.get_session(s.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.ORPHANED

    async def test_recover_no_orphans(self, storage: SQLiteStorage):
        s = Session(agent=AgentType.CLAUDE, prompt="test")
        await storage.create_session(s)
        await storage.update_session(
            s.id,
            status=SessionStatus.RUNNING,
            last_heartbeat_at=datetime.now(timezone.utc),
        )
        count = await storage.recover_orphaned_sessions(timeout_seconds=300)
        assert count == 0
