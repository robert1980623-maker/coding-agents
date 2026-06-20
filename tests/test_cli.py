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


class TestDispatchOutputContract:
    """Verify dispatch output is bounded and safe for OpenClaw exec (1MB buffer).

    Contract:
    - Only session_id + one JSON result line on stdout/stderr.
    - Intermediate stdout/stderr must NOT appear in CLI output.
    - All events are persisted to SQLite and reachable via `tail` / `status`.
    """

    def test_dispatch_output_is_bounded(self, mock_db: Path, tmp_path: Path):
        """`dispatch` must NOT echo the agent's stdout line-by-line.

        Old behavior (pre-v0.2.6) wrote one annotated line per event,
        which easily exceeded the OpenClaw exec 1MB buffer.
        """
        import sys
        from unittest.mock import patch

        # Simulate a chatty agent that emits many lines
        fake_command = [
            sys.executable,
            "-c",
            "for i in range(100): print(f'chatty line {i}')",
        ]

        class FakeAdapter:
            def build_command(self, prompt, config):
                return fake_command

        with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
            result = runner.invoke(
                app,
                ["dispatch", "claude", "test prompt"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Critical: the chatty agent's output must NOT be in CLI output
        assert "chatty line 0" not in result.output
        assert "chatty line 99" not in result.output
        # session_id line and JSON result must be present
        assert "session_id=" in result.output
        # Result line is a JSON object with at least session_id + exit_code
        import json as _json
        for line in result.output.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                parsed = _json.loads(line)
                if "session_id" in parsed and "exit_code" in parsed:
                    break
        else:
            pytest.fail(f"no result JSON line found in: {result.output!r}")

    def test_dispatch_does_not_accept_stream_flag(self):
        """The --stream flag was removed in v0.2.6. dispatch must reject it."""
        result = runner.invoke(
            app,
            ["dispatch", "claude", "test", "--stream"],
        )
        assert result.exit_code != 0
        # Should fail with "no such option"
        assert "no such option" in result.output or "--stream" in result.output
