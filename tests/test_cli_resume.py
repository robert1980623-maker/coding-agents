"""Tests for the ``coding-agents resume`` CLI command."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer.main
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()

# Repo root for subprocess-based tests (binary lives at .venv/bin/coding-agents).
REPO_ROOT = Path(__file__).resolve().parents[1]
CODING_AGENTS_BIN = REPO_ROOT / ".venv" / "bin" / "coding-agents"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch DEFAULT_DB and CODING_AGENTS_DB env var to use a temp directory."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


def _make_session(
    session_id: str = "test-session-1",
    status: SessionStatus = SessionStatus.COMPLETED,
    exit_code: Optional[int] = 0,
    agent: Any = AgentType.CLAUDE,
    prompt: str = "original task",
) -> Session:
    return Session(
        id=session_id,
        agent=agent,
        prompt=prompt,
        status=status,
        exit_code=exit_code,
    )


def _make_events(
    session_id: str,
    count: int = 3,
) -> list[Event]:
    events = []
    for i in range(1, count + 1):
        events.append(
            Event(
                session_id=session_id,
                channel="stdout" if i < count else "system",
                seq=i,
                type=EventType.STDOUT if i < count else EventType.RESULT,
                data=f"output line {i}" if i < count else json.dumps({"exit_code": 0}),
            )
        )
    return events


async def _populate_session(
    db_path: Path,
    session_id: str,
    status: SessionStatus = SessionStatus.COMPLETED,
    exit_code: Optional[int] = 0,
    event_count: int = 3,
    prompt: str = "original task",
) -> None:
    """Create a session + events in the temp db (async helper)."""
    store = SQLiteStorage(db_path)
    await store.initialize()
    session = Session(
        id=session_id,
        agent=AgentType.CLAUDE,
        prompt=prompt,
        status=status,
        exit_code=exit_code,
    )
    await store.create_session(session)
    await store.append_events(_make_events(session_id, count=event_count))
    await store.close()


# ---------------------------------------------------------------------------
# Smoke / structure
# ---------------------------------------------------------------------------


class TestResumeModuleStructure:
    def test_placeholder_is_removed(self):
        """The 19-line no-op placeholder must be gone (regression guard)."""
        path = (
            REPO_ROOT / "src" / "coding_agents" / "cli" / "resume.py"
        )
        content = path.read_text()
        line_count = content.count("\n") + 1
        assert line_count > 19, (
            f"resume.py is suspiciously short ({line_count} lines) - "
            "looks like the placeholder came back"
        )
        # The old placeholder had a register() that returned None - it should
        # now actually register a command.
        assert "return None" not in content, (
            "resume.py still contains the placeholder 'return None'"
        )

    def test_resume_command_registered(self):
        """`resume` shows up in the main app's command list."""
        click_app = typer.main.get_command(app)
        assert "resume" in click_app.commands

    def test_resume_help_works_subprocess(self):
        """End-to-end: `coding-agents resume --help` exits 0 and lists options."""
        result = subprocess.run(
            [str(CODING_AGENTS_BIN), "resume", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, (
            f"resume --help failed: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        # Key options the spec promises
        assert "--workdir" in result.stdout
        assert "--new-session-id" in result.stdout
        assert "--db" in result.stdout
        assert "--verbose" in result.stdout
        assert "--no-verbose" in result.stdout
        assert "SESSION_ID" in result.stdout or "session_id" in result.stdout


# ---------------------------------------------------------------------------
# session_id validation (synchronous, before any storage I/O)
# ---------------------------------------------------------------------------


class TestResumeSessionIdValidation:
    def test_invalid_session_id_rejected(self, mock_db: Path):
        """session_ids with spaces / slashes / quotes are rejected (exit 1)."""
        for bad in ["has space", "../etc/passwd", "x" * 200, "weird!char"]:
            result = runner.invoke(app, ["resume", bad])
            assert result.exit_code == 1, (
                f"expected exit 1 for {bad!r}, got {result.exit_code}; "
                f"output={result.stdout!r}"
            )
            assert "Invalid session_id format" in result.stdout

    def test_invalid_new_session_id_rejected(self, mock_db: Path):
        """--new-session-id is also validated."""
        result = runner.invoke(
            app, ["resume", "valid-id", "--new-session-id", "bad id!"]
        )
        assert result.exit_code == 1
        assert "Invalid --new-session-id" in result.stdout


# ---------------------------------------------------------------------------
# Pre-check failure paths
# ---------------------------------------------------------------------------


class TestResumePreCheck:
    def test_session_not_found_exits_2(self, mock_db: Path):
        """Non-existent session_id: exit code 2, friendly message."""
        result = runner.invoke(app, ["resume", "does-not-exist"])
        assert result.exit_code == 2
        assert "does-not-exist" in result.stdout
        assert "not found" in result.stdout.lower()

    def test_failed_session_not_resumable_exits_1(self, mock_db: Path):
        """A FAILED session with non-zero exit: exit 1, hint printed."""
        asyncio.run(_populate_session(
            mock_db, "failed-1", status=SessionStatus.FAILED, exit_code=1
        ))
        result = runner.invoke(app, ["resume", "failed-1"])
        assert result.exit_code == 1
        assert "Cannot resume" in result.stdout
        assert "failed" in result.stdout.lower()
        # Hint should mention retry / new session
        assert "Hint" in result.stdout or "new session" in result.stdout.lower()

    def test_running_session_not_resumable_exits_1(self, mock_db: Path):
        """A RUNNING session can't be resumed: exit 1, hint printed."""
        asyncio.run(_populate_session(
            mock_db, "running-1", status=SessionStatus.RUNNING, exit_code=None
        ))
        result = runner.invoke(app, ["resume", "running-1"])
        assert result.exit_code == 1
        assert "Cannot resume" in result.stdout
        assert "running" in result.stdout.lower()

    def test_orphaned_session_not_resumable_exits_1(self, mock_db: Path):
        """An ORPHANED session: exit 1, hint printed."""
        asyncio.run(_populate_session(
            mock_db, "orphaned-1", status=SessionStatus.ORPHANED, exit_code=None
        ))
        result = runner.invoke(app, ["resume", "orphaned-1"])
        assert result.exit_code == 1
        assert "Cannot resume" in result.stdout

    def test_pending_session_not_resumable_exits_1(self, mock_db: Path):
        """A PENDING session (no status update yet): exit 1."""
        asyncio.run(_populate_session(
            mock_db, "pending-1", status=SessionStatus.PENDING, exit_code=None
        ))
        result = runner.invoke(app, ["resume", "pending-1"])
        assert result.exit_code == 1
        assert "Cannot resume" in result.stdout

    def test_no_events_session_not_resumable_exits_1(self, mock_db: Path):
        """A completed session with no events: exit 1 (can_resume = False)."""
        asyncio.run(_populate_session(
            mock_db, "empty-1", status=SessionStatus.COMPLETED, event_count=0
        ))
        result = runner.invoke(app, ["resume", "empty-1"])
        assert result.exit_code == 1
        assert "Cannot resume" in result.stdout


# ---------------------------------------------------------------------------
# Successful resume path (with mocked agent factory)
# ---------------------------------------------------------------------------


class TestResumeSuccess:
    """End-to-end successful resume, with a mock agent factory and executor
    so we never spawn a real claude/codex subprocess.
    """

    def _patch_resume_core(
        self,
        resume_events: list[Event],
        captured_commands: Optional[list[list[str]]] = None,
        agent_command: Optional[list[str]] = None,
    ) -> Any:
        """Return a context-manager that patches coding_agents.resume
        ``get_agent`` and ``StreamExecutor`` so resume_session() is
        fully synchronous w.r.t. tests.
        """
        mock_agent = MagicMock()
        mock_agent.build_command.return_value = agent_command or [
            "claude", "-p", "original task"
        ]

        mock_executor_instance = MagicMock()

        async def mock_execute(
            session_id: str,
            command: list[str],
            workdir: str,
            env: Optional[dict[str, str]] = None,
        ) -> AsyncIterator[Event]:
            if captured_commands is not None:
                captured_commands.append(command)
            for ev in resume_events:
                # Write each event to the new session's storage so the CLI's
                # polling loop picks it up. The CLI uses storage polling for
                # real-time streaming, so this faithfully mimics the real
                # executor's behavior of writing events to SQLite.
                store = SQLiteStorage(os.environ["CODING_AGENTS_DB"])
                await store.initialize()
                await store.append_events([ev])
                # Finalize the session to terminal status so the CLI's
                # can_resume / get_resume_info paths work on the new session
                # and so the polling loop's terminal check exits.
                if ev.type == EventType.RESULT:
                    await store.update_session(
                        session_id,
                        status=SessionStatus.COMPLETED,
                        exit_code=0,
                        finished_at=datetime.now(timezone.utc),
                    )
                await store.close()
                yield ev

        mock_executor_instance.execute = mock_execute

        return (
            patch("coding_agents.resume.get_agent", return_value=mock_agent),
            patch(
                "coding_agents.resume.StreamExecutor",
                return_value=mock_executor_instance,
            ),
        )

    def test_successful_resume_exits_0(self, mock_db: Path):
        """Resume a COMPLETED session: exit 0, summary printed, new session in storage."""
        asyncio.run(_populate_session(
            mock_db, "orig-1", status=SessionStatus.COMPLETED, exit_code=0
        ))

        # Build the events the mock executor will yield for the *new* session.
        new_events = _make_events("placeholder", count=3)
        # Replace session_id with one we'll know up-front (the CLI pre-allocates).
        new_events = [
            Event(
                session_id="new-fixed-id",
                channel=ev.channel,
                seq=ev.seq,
                type=ev.type,
                data=ev.data,
            )
            for ev in new_events
        ]

        patch_agent, patch_executor = self._patch_resume_core(
            new_events, agent_command=["claude", "-p", "original task"]
        )

        with patch_agent, patch_executor:
            result = runner.invoke(
                app,
                [
                    "resume", "orig-1",
                    "--new-session-id", "new-fixed-id",
                ],
            )

        assert result.exit_code == 0, (
            f"expected exit 0, got {result.exit_code}; "
            f"stdout={result.stdout!r}"
        )
        # Header
        assert "Resuming session orig-1" in result.stdout
        # Summary
        assert "Resume complete" in result.stdout
        assert "new-fixed-id" in result.stdout
        assert "Event count" in result.stdout
        assert "Final status" in result.stdout

        # New session is in storage, linked to original
        async def _check():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            new_session = await store.get_session("new-fixed-id")
            assert new_session is not None
            assert new_session.metadata.get("resumed_from") == "orig-1"
            # Workdir flag was passed (default ".") and recorded
            assert new_session.workdir == "."
            await store.close()

        asyncio.run(_check())

    def test_resume_command_uses_resume_flag(self, mock_db: Path):
        """The captured command must include the original session_id via --resume."""
        asyncio.run(_populate_session(
            mock_db, "orig-2", status=SessionStatus.KILLED, exit_code=None
        ))

        captured: list[list[str]] = []
        new_events = [
            Event(
                session_id="new-2",
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
        ]
        patch_agent, patch_executor = self._patch_resume_core(
            new_events, captured_commands=captured
        )

        with patch_agent, patch_executor:
            result = runner.invoke(
                app,
                [
                    "resume", "orig-2",
                    "--new-session-id", "new-2",
                ],
            )

        assert result.exit_code == 0
        assert len(captured) == 1
        cmd = captured[0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "orig-2"

    def test_resume_with_killed_status_succeeds(self, mock_db: Path):
        """KILLED sessions are in RESUMABLE_STATUSES and should resume."""
        asyncio.run(_populate_session(
            mock_db, "killed-1", status=SessionStatus.KILLED, exit_code=None
        ))

        new_events = [
            Event(
                session_id="new-k",
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
        ]
        patch_agent, patch_executor = self._patch_resume_core(new_events)

        with patch_agent, patch_executor:
            result = runner.invoke(
                app, ["resume", "killed-1", "--new-session-id", "new-k"]
            )
        assert result.exit_code == 0
        assert "Resume complete" in result.stdout

    def test_resume_with_timeout_status_succeeds(self, mock_db: Path):
        """TIMEOUT sessions are in RESUMABLE_STATUSES and should resume."""
        asyncio.run(_populate_session(
            mock_db, "to-1", status=SessionStatus.TIMEOUT, exit_code=None
        ))

        new_events = [
            Event(
                session_id="new-to",
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
        ]
        patch_agent, patch_executor = self._patch_resume_core(new_events)

        with patch_agent, patch_executor:
            result = runner.invoke(
                app, ["resume", "to-1", "--new-session-id", "new-to"]
            )
        assert result.exit_code == 0
        assert "Resume complete" in result.stdout

    def test_resume_duplicate_new_session_id_rejected(self, mock_db: Path):
        """--new-session-id that already exists in storage: exit 1."""
        # Seed two sessions: orig + the one we want to collide with.
        asyncio.run(_populate_session(
            mock_db, "orig-dup", status=SessionStatus.COMPLETED
        ))
        asyncio.run(_populate_session(
            mock_db, "already-here", status=SessionStatus.COMPLETED
        ))

        result = runner.invoke(
            app,
            ["resume", "orig-dup", "--new-session-id", "already-here"],
        )
        assert result.exit_code == 1
        assert "already exists" in result.stdout

    def test_verbose_flag_accepted(self, mock_db: Path):
        """--verbose is parsed and accepted by typer."""
        asyncio.run(_populate_session(
            mock_db, "orig-v", status=SessionStatus.COMPLETED
        ))
        new_events = [
            Event(
                session_id="new-v",
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
        ]
        patch_agent, patch_executor = self._patch_resume_core(new_events)

        with patch_agent, patch_executor:
            result = runner.invoke(
                app,
                [
                    "resume", "orig-v",
                    "--new-session-id", "new-v",
                    "--verbose",
                ],
            )
        assert result.exit_code == 0
