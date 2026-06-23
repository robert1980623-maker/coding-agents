"""Tests for the ``watch`` CLI command."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


async def _fast_sleep(_seconds: float) -> None:
    """Test helper: instant sleep so the watch tests don't wait minutes.

    Must NOT call asyncio.sleep itself — this helper is used to
    patch coding_agents.cli.watch.asyncio.sleep, so any nested
    asyncio.sleep call would recurse into the patched version.
    """
    return None



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

        # Simulate status transitions in a background thread.
        # v0.2.30+: We use time.sleep (not asyncio.sleep) here so
        # the patch on coding_agents.cli.watch.asyncio.sleep
        # doesn't affect the bg thread's sleep.

        def _bg_transition() -> None:
            # Pre-set to running so watch sees it on the first poll
            asyncio.run(_update_status(sid, SessionStatus.RUNNING))
            time.sleep(0.3)  # let watch print the initial status
            # Then transition to completed
            asyncio.run(_update_status(sid, SessionStatus.COMPLETED))

        async def _update_status(session_id: str, status: SessionStatus) -> None:
            store = SQLiteStorage(mock_db)
            await store.initialize()
            await store.update_session(session_id, status=status)
            await store.close()

        thread = threading.Thread(target=_bg_transition)
        thread.start()

        # v0.2.30+: minimum interval is 300s. The CLI check is
        # done up-front via _validate_interval. We patch the validator
        # to allow short intervals in tests, and mock asyncio.sleep in
        # the watch module so the test doesn't actually wait 5 minutes
        # for the second poll cycle.
        with patch("coding_agents.cli.watch._validate_interval"), \
             patch("coding_agents.cli.watch.asyncio.sleep", new=_fast_sleep):
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
        # Use a very short timeout so the test finishes quickly.
        # v0.2.30+: minimum interval is 300s enforced by _validate_interval;
        # we patch the validator for testing and mock asyncio.sleep to instant.
        with patch("coding_agents.cli.watch._validate_interval"), \
             patch("coding_agents.cli.watch.asyncio.sleep", new=_fast_sleep):
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
        result = runner.invoke(app, ["watch", sid, "--interval", "600"])
        assert result.exit_code == 0

    def test_watch_short_interval_option(self, mock_db: Path):
        """The -i short option for --interval should be accepted."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "-i", "600"])
        assert result.exit_code == 0

    def test_watch_interval_below_minimum_rejected(self, mock_db: Path):
        """v0.2.30+: --interval < 300s is rejected to enforce provider quota floor."""
        sid = asyncio.run(_create_session(mock_db, SessionStatus.COMPLETED))
        result = runner.invoke(app, ["watch", sid, "--interval", "60"])
        assert result.exit_code != 0
        assert "300" in result.stdout or "300" in (result.stderr or "")

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
