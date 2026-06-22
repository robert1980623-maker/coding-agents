"""Tests for the ``watch`` CLI command."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
def mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch DEFAULT_DB and CODING_AGENTS_DB env var to use a temp directory."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


async def _create_session(db_path: Path, status: SessionStatus = SessionStatus.PENDING) -> str:
    """Helper: create a session with the given status."""
    store = SQLiteStorage(db_path)
    await store.initialize()
    s = Session(agent=AgentType.CLAUDE, prompt="test prompt", workdir="/tmp")
    await store.create_session(s)
    if status != SessionStatus.PENDING:
        await store.update_session(s.id, status=status)
    await store.close()
    return s.id


class TestWatchCommand:
    def test_watch_terminal_session_exits_immediately(self, mock_db: Path):
        """Watching an already-terminal session should exit immediately with code 0."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid])
        assert result.exit_code == 0
        assert "completed" in result.stdout.lower()

    def test_watch_nonexistent_session(self, mock_db: Path):
        """Watching a non-existent session should exit with code 1."""
        result = runner.invoke(app, ["watch", "does-not-exist"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_watch_status_transition(self, mock_db: Path):
        """Watching a session that transitions from pending → running → completed."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.PENDING))

        # Simulate status transitions in a background thread
        transitions_done = asyncio.Event()

        async def _transition():
            await asyncio.sleep(0.3)  # Let watch start polling
            store = SQLiteStorage(mock_db)
            await store.initialize()
            await store.update_session(sid, status=SessionStatus.RUNNING)
            await asyncio.sleep(0.3)
            await store.update_session(sid, status=SessionStatus.COMPLETED)
            await store.close()

        # Run transitions concurrently with watch
        async def _run_both():
            task = asyncio.create_task(_transition())
            # We can't directly invoke the CLI in async, but we can test
            # the underlying logic via the storage layer
            await task

        # For the CLI test, we pre-transition the session
        async def _setup_and_run():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            await store.update_session(sid, status=SessionStatus.RUNNING)
            await store.close()

        asyncio.run(_setup_and_run())

        # Now watch with a short interval; the session is running, will
        # transition to completed via a background task. Use threading to
        # drive the transition while watch polls.
        import threading

        def _bg_transition():
            async def _inner():
                await asyncio.sleep(0.2)
                store = SQLiteStorage(mock_db)
                await store.initialize()
                await store.update_session(sid, status=SessionStatus.COMPLETED)
                await store.close()
            asyncio.run(_inner())

        thread = threading.Thread(target=_bg_transition)
        thread.start()

        result = runner.invoke(app, ["watch", sid, "--interval", "1", "--timeout", "5"])
        thread.join(timeout=5)

        assert result.exit_code == 0
        # Should see both the initial running status and the transition to completed
        assert "running" in result.stdout.lower()
        assert "completed" in result.stdout.lower()
        # Should see the transition arrow
        assert "→" in result.stdout or "->" in result.stdout

    def test_watch_timeout_exits_with_code_1(self, mock_db: Path):
        """Watching a non-terminal session past the timeout should exit with code 1."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.RUNNING))
        # Use a very short timeout so the test finishes quickly
        result = runner.invoke(app, ["watch", sid, "--interval", "1", "--timeout", "1"])
        assert result.exit_code == 1
        assert "timeout" in result.stdout.lower()

    def test_watch_failed_session(self, mock_db: Path):
        """Watching a session that has failed should exit with code 0."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.FAILED))
        result = runner.invoke(app, ["watch", sid])
        assert result.exit_code == 0
        assert "failed" in result.stdout.lower()

    def test_watch_killed_session(self, mock_db: Path):
        """Watching a killed session should exit with code 0."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.KILLED))
        result = runner.invoke(app, ["watch", sid])
        assert result.exit_code == 0
        assert "killed" in result.stdout.lower()

    def test_watch_interval_option_accepted(self, mock_db: Path):
        """The --interval option should be accepted without error."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "--interval", "60"])
        assert result.exit_code == 0

    def test_watch_short_interval_option(self, mock_db: Path):
        """The -i short option for --interval should be accepted."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "-i", "60"])
        assert result.exit_code == 0

    def test_watch_timeout_option_accepted(self, mock_db: Path):
        """The --timeout option should be accepted without error."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "--timeout", "60"])
        assert result.exit_code == 0

    def test_watch_short_timeout_option(self, mock_db: Path):
        """The -t short option for --timeout should be accepted."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "-t", "60"])
        assert result.exit_code == 0

    def test_watch_output_format_has_timestamp(self, mock_db: Path):
        """Output should contain a timestamp in [YYYY-MM-DD HH:MM:SS] format."""
        import re
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid])
        assert result.exit_code == 0
        # Check for timestamp pattern like [2024-01-01 12:00:00]
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result.stdout)

    def test_watch_output_format_has_status(self, mock_db: Path):
        """Output should contain 'Status:' followed by the status value."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid])
        assert result.exit_code == 0
        assert "Status:" in result.stdout
        assert "completed" in result.stdout.lower()
