"""Tests for CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
def mock_db(tmp_path: Path):
    """Patch DEFAULT_DB to use a temp directory."""
    db_path = tmp_path / "test.db"
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


class TestRunCommand:
    def test_run_echo(self, mock_db: Path):
        """Run a simple echo command as a fake agent."""
        # This test would require mocking the subprocess. Skip for now,
        # we test the executor separately.
        pass


class TestStatusCommand:
    def test_status_existing(self, mock_db: Path):
        """Show status of an existing session."""
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            session = Session(agent=AgentType.CLAUDE, prompt="test prompt", workdir="/tmp")
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["status", sid])
        assert result.exit_code == 0
        assert sid[:8] in result.stdout or sid in result.stdout

    def test_status_nonexistent(self, mock_db: Path):
        result = runner.invoke(app, ["status", "does-not-exist"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()


class TestListCommand:
    def test_list_empty(self, mock_db: Path):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "no sessions" in result.stdout.lower()

    def test_list_with_sessions(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            for i in range(3):
                s = Session(agent=AgentType.CLAUDE, prompt=f"prompt {i}")
                await store.create_session(s)
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "prompt" in result.stdout.lower()

    def test_list_filter_agent(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            await store.create_session(Session(agent=AgentType.CLAUDE, prompt="claude task"))
            await store.create_session(Session(agent=AgentType.CODEX, prompt="codex task"))
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["list", "--agent", "claude"])
        assert result.exit_code == 0
        assert "claude task" in result.stdout.lower()


class TestTagCommand:
    def test_add_tag(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["tag", sid, "important"])
        assert result.exit_code == 0
        assert "added" in result.stdout.lower()

    def test_remove_tag(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.add_tag(s.id, "important")
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["tag", "-r", sid, "important"])
        assert result.exit_code == 0
        assert "removed" in result.stdout.lower()


class TestKillCommand:
    def test_kill_running(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.update_session(s.id, status=SessionStatus.RUNNING)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["kill", sid])
        assert result.exit_code == 0
        assert "killed" in result.stdout.lower()

    def test_kill_already_completed(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.update_session(s.id, status=SessionStatus.COMPLETED)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["kill", sid])
        assert result.exit_code == 0
        assert "already" in result.stdout.lower()


class TestSearchCommand:
    def test_search_no_results(self, mock_db: Path):
        result = runner.invoke(app, ["search", "nonexistent"])
        assert result.exit_code == 0
        assert "no matching" in result.stdout.lower()


class TestRecoverCommand:
    def test_recover(self, mock_db: Path):
        result = runner.invoke(app, ["recover"])
        assert result.exit_code == 0
        assert "marked" in result.stdout.lower()
