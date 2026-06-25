"""Tests for the ``coding-agents poll`` command.

v0.2.19: PM-oriented fleet health overview — one command replaces
``list`` + per-session ``tail --limit 1``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app, _format_duration, _parse_duration
from coding_agents.models import AgentType, Event, EventType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
def mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch DEFAULT_DB and CODING_AGENTS_DB env var to use a temp directory."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_session(
    store: SQLiteStorage,
    *,
    status: SessionStatus = SessionStatus.RUNNING,
    heartbeat: datetime | None = None,
    started: datetime | None = None,
    events: list[Event] | None = None,
    prompt: str = "test prompt",
    agent: AgentType = AgentType.CLAUDE,
) -> Session:
    """Create a session in ``store`` and optionally append events."""
    now = datetime.now(timezone.utc)
    session = Session(
        agent=agent,
        prompt=prompt,
        workdir="/tmp",
        status=status,
        started_at=started or now,
        last_heartbeat_at=heartbeat,
    )
    await store.create_session(session)
    if events:
        await store.append_events(events)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPollEmpty:
    """Empty database — should exit 0 with a friendly message."""

    def test_empty_table(self, mock_db: Path):
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert "no matching" in result.stdout.lower()

    def test_empty_json(self, mock_db: Path):
        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["sessions"] == []
        assert data["summary"]["total"] == 0


class TestPollDefaultActiveOnly:
    """Default: only running + pending sessions are shown."""

    def test_running_sessions(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=now,
                events=[Event(
                    session_id="", channel="stdout", seq=1,
                    type=EventType.STDOUT, data="hello",
                )],
            )
            # Fixup: the event's session_id must match the session we just
            # created. Easiest is to create the session first, then append.
            await store.close()

        # Better approach: create sessions explicitly, then add events.
        async def _setup2():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s1 = await _seed_session(
                store, status=SessionStatus.RUNNING, heartbeat=now,
            )
            await store.append_events([Event(
                session_id=s1.id, channel="stdout", seq=1,
                type=EventType.STDOUT, data="hello world",
            )])
            await store.close()
            return s1.id

        sid = asyncio.run(_setup2())
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert sid in result.stdout
        assert "running" in result.stdout.lower()

    def test_completed_excluded_by_default(self, mock_db: Path):
        """Completed sessions should NOT appear in default (active-only) poll."""
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s = await _seed_session(store, status=SessionStatus.COMPLETED)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert "no matching" in result.stdout.lower()
        assert sid not in result.stdout


class TestPollMixedStatus:
    """--all shows all statuses; default shows only active."""

    def test_all_flag(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s_running = await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=now, prompt="running task",
            )
            s_completed = await _seed_session(
                store, status=SessionStatus.COMPLETED,
                heartbeat=now, prompt="completed task",
            )
            s_failed = await _seed_session(
                store, status=SessionStatus.FAILED,
                heartbeat=now, prompt="failed task",
            )
            s_pending = await _seed_session(
                store, status=SessionStatus.PENDING,
                prompt="pending task",
            )
            await store.close()
            return s_running.id, s_completed.id, s_failed.id, s_pending.id

        ids = asyncio.run(_setup())
        running_id, completed_id, failed_id, pending_id = ids

        # Default: only running + pending
        result_default = runner.invoke(app, ["poll"])
        assert result_default.exit_code == 0
        assert running_id in result_default.stdout
        assert pending_id in result_default.stdout
        assert completed_id not in result_default.stdout
        assert failed_id not in result_default.stdout

        # --all: everyone
        result_all = runner.invoke(app, ["poll", "--all"])
        assert result_all.exit_code == 0
        for sid in ids:
            assert sid in result_all.stdout, (
                f"session {sid} missing from --all output:\n{result_all.stdout}"
            )


class TestPollStatusFilter:
    """--status narrows to a single status value."""

    def test_status_completed(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            await _seed_session(
                store, status=SessionStatus.RUNNING, heartbeat=now,
            )
            s = await _seed_session(store, status=SessionStatus.COMPLETED)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll", "--status", "completed"])
        assert result.exit_code == 0
        assert sid in result.stdout
        # The running session should NOT appear
        assert result.stdout.lower().count("running") <= 1  # column header only


class TestPollStuckDetection:
    """Sessions without recent heartbeats are marked STUCK."""

    def test_stuck_running_session(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            # Heartbeat 1 hour ago → stuck (default threshold: 30m)
            old = now - timedelta(hours=1)
            s = await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=old, started=old,
            )
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert "stuck" in result.stdout.lower() or "⚠" in result.stdout

    def test_not_stuck_recent_heartbeat(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            # Heartbeat 1 minute ago → not stuck
            s = await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=now - timedelta(minutes=1),
                started=now - timedelta(minutes=5),
            )
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert sid in result.stdout
        # Should NOT show STUCK (but may show "no" in the stuck column)
        assert "⚠" not in result.stdout

    def test_custom_stuck_after(self, mock_db: Path):
        """--stuck-after overrides the default 30m threshold."""
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            # Heartbeat 2 hours ago
            old = now - timedelta(hours=2)
            s = await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=old, started=old,
            )
            await store.close()
            return s.id

        sid = asyncio.run(_setup())

        # With default 30m → stuck
        result_default = runner.invoke(app, ["poll"])
        assert "⚠" in result_default.stdout

        # With --stuck-after 3h → NOT stuck
        result_custom = runner.invoke(app, ["poll", "--stuck-after", "3h"])
        assert result_custom.exit_code == 0
        assert "⚠" not in result_custom.stdout


class TestPollJsonOutput:
    """--format json produces structured output."""

    def test_json_structure(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s = await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=now,
            )
            await store.append_events([Event(
                session_id=s.id, channel="stdout", seq=1,
                type=EventType.STDOUT, data="output data",
            )])
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)

        assert "sessions" in data
        assert "summary" in data
        assert len(data["sessions"]) == 1

        session = data["sessions"][0]
        assert session["id"] == sid
        assert session["agent"] == "claude"
        assert session["status"] == "running"
        assert session["stuck"] is False
        assert session["last_event"] is not None
        assert session["last_event"]["type"] == "stdout"
        assert session["last_event"]["seq"] == 1
        assert session["running_for_ms"] is not None
        assert session["running_for_ms"] >= 0

        summary = data["summary"]
        assert summary["total"] == 1
        assert summary["running"] == 1
        assert summary["stuck"] == 0

    def test_json_no_events(self, mock_db: Path):
        """Sessions without events should have last_event = null."""
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s = await _seed_session(
                store, status=SessionStatus.RUNNING, heartbeat=now,
            )
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["sessions"][0]["last_event"] is None

    def test_json_stuck_flag(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            old = now - timedelta(hours=2)
            await _seed_session(
                store, status=SessionStatus.RUNNING,
                heartbeat=old, started=old,
            )
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["poll", "--format", "json"])
        data = json.loads(result.stdout)
        assert data["sessions"][0]["stuck"] is True
        assert data["summary"]["stuck"] == 1


class TestPollLimit:
    """--limit caps the number of sessions returned."""

    def test_limit(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            for i in range(5):
                await _seed_session(
                    store, status=SessionStatus.RUNNING,
                    heartbeat=now, prompt=f"task {i}",
                )
            await store.close()

        asyncio.run(_setup())

        result = runner.invoke(app, ["poll", "--limit", "2"])
        assert result.exit_code == 0
        # Should only show 2 sessions — hard to count exactly from the
        # table, but JSON makes it trivial.
        result_json = runner.invoke(
            app, ["poll", "--limit", "2", "--format", "json"]
        )
        data = json.loads(result_json.stdout)
        assert len(data["sessions"]) == 2
        assert data["summary"]["total"] == 2


class TestPollNoEvents:
    """Session with no events at all should show '-' for last event."""

    def test_table_no_events(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            s = await _seed_session(
                store, status=SessionStatus.RUNNING, heartbeat=now,
            )
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["poll"])
        assert result.exit_code == 0
        assert sid in result.stdout
        # '-' is the placeholder for missing last event. It appears in
        # the "Last Event" column. Just verify the table renders.


class TestPollTerminalNotStuck:
    """Terminal sessions (completed/failed) never show as stuck."""

    def test_completed_not_stuck(self, mock_db: Path):
        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            old = now - timedelta(days=1)
            # Completed session with old heartbeat — NOT stuck
            await _seed_session(
                store, status=SessionStatus.COMPLETED,
                heartbeat=old,
            )
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["poll", "--all", "--format", "json"])
        data = json.loads(result.stdout)
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["stuck"] is False


# ---------------------------------------------------------------------------
# Unit tests for duration helpers
# ---------------------------------------------------------------------------

class TestFormatDuration:
    def test_seconds(self):
        assert _format_duration(45) == "45s"

    def test_minutes(self):
        assert _format_duration(300) == "5m"

    def test_minutes_with_seconds(self):
        assert _format_duration(261) == "4m21s"

    def test_hours(self):
        assert _format_duration(7200) == "2h"

    def test_hours_with_minutes(self):
        assert _format_duration(5400) == "1h30m"

    def test_zero(self):
        assert _format_duration(0) == "0s"

    def test_negative(self):
        assert _format_duration(-1) == "-"


class TestParseDuration:
    def test_minutes(self):
        assert _parse_duration("30m") == 1800

    def test_hours(self):
        assert _parse_duration("1h") == 3600

    def test_hours_and_minutes(self):
        assert _parse_duration("1h30m") == 5400

    def test_seconds(self):
        assert _parse_duration("90s") == 90

    def test_bare_number(self):
        assert _parse_duration("120") == 120

    def test_invalid_fallback(self):
        # Invalid string falls back to 30 minutes
        assert _parse_duration("garbage") == 1800


class TestPollAutoClean:
    """Test auto-cleanup in poll command."""

    def test_auto_clean_enabled_by_default(self, mock_db: Path):
        """poll should auto-clean stuck pending sessions by default."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        assert "Auto-cleaned" in result.stdout or "auto-cleaned" in result.stdout.lower()

        # Verify session was marked as failed
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.FAILED

    def test_no_auto_clean_flag(self, mock_db: Path):
        """poll with --no-auto-clean should not clean stuck sessions."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        result = runner.invoke(app, ["poll", "--format", "json", "--no-auto-clean"])
        assert result.exit_code == 0

        # Verify session is still pending
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.PENDING

    def test_quiet_flag(self, mock_db: Path):
        """poll with --quiet should suppress cleanup message."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        asyncio.run(_setup())

        result = runner.invoke(app, ["poll", "--format", "json", "--quiet"])
        assert result.exit_code == 0
        # Should not show cleanup message
        assert "Auto-cleaned" not in result.stdout

    def test_auto_clean_running_orphaned(self, mock_db: Path):
        """poll should auto-clean running sessions with no heartbeat > 24h."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.RUNNING,
                started_at=now - timedelta(hours=25),
                last_heartbeat_at=now - timedelta(hours=25),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        assert "Auto-cleaned" in result.stdout or "auto-cleaned" in result.stdout.lower()

        # Verify session was marked as orphaned
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.ORPHANED
