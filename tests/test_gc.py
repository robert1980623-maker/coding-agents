"""Tests for v0.2.6: gc / tail / status bounded output."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from click.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Event, EventType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
async def storage(tmp_path: Path):
    """Fresh SQLite storage in a tmp dir."""
    db_path = tmp_path / "test.db"
    s = SQLiteStorage(db_path)
    await s.initialize()
    yield s
    await s.close()


def _make_session(**overrides) -> Session:
    defaults = dict(
        agent=AgentType.CLAUDE,
        prompt="test",
        workdir="/tmp",
        model=None,
    )
    defaults.update(overrides)
    return Session(**defaults)


@pytest.mark.asyncio
async def test_get_latest_events_returns_newest_first(storage: SQLiteStorage):
    """get_latest_events should return the last N events, oldest-first."""
    session = _make_session()
    await storage.create_session(session)
    # Append 50 events with unique seq
    events = [
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=i, data=f"line {i}")
        for i in range(50)
    ]
    await storage.append_events(events)

    # Ask for last 10
    latest = await storage.get_latest_events(session.id, limit=10)
    assert len(latest) == 10
    # Should be the LAST 10 (seq 40..49), oldest-first
    assert latest[0].seq == 40
    assert latest[-1].seq == 49


@pytest.mark.asyncio
async def test_get_latest_events_fewer_than_limit(storage: SQLiteStorage):
    """If session has fewer events than limit, return all of them."""
    session = _make_session()
    await storage.create_session(session)
    events = [
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=i, data=f"line {i}")
        for i in range(5)
    ]
    await storage.append_events(events)

    latest = await storage.get_latest_events(session.id, limit=20)
    assert len(latest) == 5


@pytest.mark.asyncio
async def test_delete_session_removes_everything(storage: SQLiteStorage):
    """delete_session should remove the session, its events, and its tags."""
    session = _make_session()
    await storage.create_session(session)
    await storage.add_tag(session.id, "test-tag")
    events = [
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=1, data="data")
    ]
    await storage.append_events(events)

    # Verify it's there
    assert await storage.get_session(session.id) is not None
    assert await storage.list_tags(session.id) == ["test-tag"]
    assert len(await storage.get_events(session.id)) == 1

    # Delete
    await storage.delete_session(session.id)

    # Verify it's gone
    assert await storage.get_session(session.id) is None
    assert await storage.list_tags(session.id) == []
    assert await storage.get_events(session.id) == []


@pytest.mark.asyncio
async def test_prune_events_keep_result(storage: SQLiteStorage):
    """prune_events_keep_result should drop stdout/stderr, keep result."""
    session = _make_session()
    await storage.create_session(session)

    # Mix of event types
    events = [
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=1, data="out1"),
        Event(session_id=session.id, channel="stderr", type=EventType.STDERR, seq=2, data="err1"),
        Event(session_id=session.id, channel="system", type=EventType.SESSION_START, seq=3, data="{}"),
        Event(session_id=session.id, channel="system", type=EventType.RESULT, seq=4, data='{"exit_code": 0}'),
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=5, data="out2"),
    ]
    await storage.append_events(events)

    # Prune
    deleted = await storage.prune_events_keep_result(session.id)
    assert deleted == 4  # all except result

    # Verify only result remains
    remaining = await storage.get_events(session.id)
    assert len(remaining) == 1
    assert remaining[0].type == EventType.RESULT


@pytest.mark.asyncio
async def test_vacuum_runs_without_error(storage: SQLiteStorage):
    """vacuum() should complete without raising."""
    session = _make_session()
    await storage.create_session(session)
    events = [
        Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=i, data="x" * 1000)
        for i in range(100)
    ]
    await storage.append_events(events)
    await storage.delete_session(session.id)
    # Should not raise
    await storage.vacuum()


def test_status_command_shows_recent_events(tmp_path: Path, monkeypatch):
    """`status <id>` should show session metadata + last N events."""
    # Use a fresh DB in tmp_path
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))

    # Manually populate
    import asyncio
    async def setup():
        s = SQLiteStorage(db_path)
        await s.initialize()
        session = _make_session()
        await s.create_session(session)
        events = [
            Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=i, data=f"line {i}")
            for i in range(30)
        ]
        await s.append_events(events)
        await s.update_session(session.id, status=SessionStatus.COMPLETED, finished_at=datetime.now(timezone.utc))
        await s.close()
        return session.id

    sid = asyncio.run(setup())

    # Run status
    import typer.main
    click_app = typer.main.get_command(app)
    result = runner.invoke(click_app, ["status", sid])
    assert result.exit_code == 0
    # Should show session metadata
    assert "Session ID" in result.output or sid in result.output
    # Should show "recent 20 event(s)"
    assert "recent 20 event(s)" in result.output


def test_tail_command_shows_events(tmp_path: Path, monkeypatch):
    """`tail <id>` should show the most recent events (default 100)."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))

    import asyncio
    async def setup():
        s = SQLiteStorage(db_path)
        await s.initialize()
        session = _make_session()
        await s.create_session(session)
        events = [
            Event(session_id=session.id, channel="stdout", type=EventType.STDOUT, seq=i, data=f"line {i}")
            for i in range(150)
        ]
        await s.append_events(events)
        await s.close()
        return session.id

    sid = asyncio.run(setup())

    import typer.main
    click_app = typer.main.get_command(app)
    result = runner.invoke(click_app, ["tail", sid])
    assert result.exit_code == 0
    # Should show "100 most recent event(s)"
    assert "100 most recent event(s)" in result.output


def test_gc_dry_run(tmp_path: Path, monkeypatch):
    """`gc --dry-run` should report what would be deleted without deleting."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))

    import asyncio
    async def setup():
        s = SQLiteStorage(db_path)
        await s.initialize()
        # Create a completed session 60 days ago
        session = _make_session()
        await s.create_session(session)
        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        await s.update_session(
            session.id,
            status=SessionStatus.COMPLETED,
            finished_at=old_time,
        )
        await s.close()
        return session.id

    sid = asyncio.run(setup())

    # Dry run
    import typer.main
    click_app = typer.main.get_command(app)
    result = runner.invoke(click_app, ["gc", "--dry-run", "--older-than", "30"])
    assert result.exit_code == 0
    assert "would drop 1 session(s)" in result.output

    # Verify session still exists
    async def check():
        s = SQLiteStorage(db_path)
        await s.initialize()
        session = await s.get_session(sid)
        await s.close()
        return session is not None

    assert asyncio.run(check()) is True
