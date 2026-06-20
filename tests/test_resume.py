"""Tests for session resume functionality."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.resume import (
    ResumeInfo,
    ResumeNotSupportedError,
    can_resume,
    get_resume_info,
    prepare_resume_command,
    resume_session,
)
from coding_agents.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# can_resume
# ---------------------------------------------------------------------------

class TestCanResume:
    async def test_nonexistent_session(self, storage: SQLiteStorage):
        assert await can_resume("does-not-exist", storage) is False

    async def test_completed_session_with_events(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.COMPLETED, exit_code=0)
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=3))

        assert await can_resume(session.id, storage) is True

    async def test_killed_session_is_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.KILLED)
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=2))

        assert await can_resume(session.id, storage) is True

    async def test_timeout_session_is_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.TIMEOUT)
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=2))

        assert await can_resume(session.id, storage) is True

    async def test_failed_nonzero_exit_not_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(
            status=SessionStatus.FAILED, exit_code=1
        )
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=2))

        assert await can_resume(session.id, storage) is False

    async def test_failed_not_resumable(
        self, storage: SQLiteStorage
    ):
        """FAILED is not resumable regardless of exit code (agent state unreliable)."""
        session = _make_session(
            status=SessionStatus.FAILED, exit_code=0
        )
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=2))

        assert await can_resume(session.id, storage) is False

    async def test_orphaned_not_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.ORPHANED)
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=1))

        assert await can_resume(session.id, storage) is False

    async def test_running_not_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.RUNNING)
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id, count=1))

        assert await can_resume(session.id, storage) is False

    async def test_no_events_not_resumable(
        self, storage: SQLiteStorage
    ):
        session = _make_session(status=SessionStatus.COMPLETED)
        await storage.create_session(session)
        # No events appended

        assert await can_resume(session.id, storage) is False


# ---------------------------------------------------------------------------
# get_resume_info
# ---------------------------------------------------------------------------

class TestGetResumeInfo:
    async def test_returns_info(self, storage: SQLiteStorage):
        session = _make_session(
            session_id="s1",
            status=SessionStatus.COMPLETED,
            agent=AgentType.CLAUDE,
            prompt="do the thing",
        )
        await storage.create_session(session)
        events = _make_events(session.id, count=5)
        await storage.append_events(events)

        info = await get_resume_info(session.id, storage)
        assert info is not None
        assert info.session_id == "s1"
        assert info.last_seq == 5
        assert info.agent_type == AgentType.CLAUDE
        assert info.prompt == "do the thing"
        assert info.exit_code == 0

    async def test_nonexistent_returns_none(self, storage: SQLiteStorage):
        assert await get_resume_info("nope", storage) is None

    async def test_no_events_returns_none(self, storage: SQLiteStorage):
        session = _make_session()
        await storage.create_session(session)
        assert await get_resume_info(session.id, storage) is None


# ---------------------------------------------------------------------------
# prepare_resume_command
# ---------------------------------------------------------------------------

class TestPrepareResumeCommand:
    def test_claude_resume(self):
        session = _make_session(session_id="my-session")
        mock_agent = MagicMock()
        mock_agent.build_command.return_value = [
            "claude", "-p", "do stuff"
        ]

        with patch("coding_agents.resume.get_agent", return_value=mock_agent):
            cmd = prepare_resume_command(
                session, AgentType.CLAUDE, ExecutionConfig(), last_seq=10
            )

        assert cmd == ["claude", "-p", "do stuff", "--resume", "my-session"]

    def test_codex_resume(self):
        session = _make_session(session_id="my-session")
        mock_agent = MagicMock()
        mock_agent.build_command.return_value = [
            "codex", "exec", "do stuff"
        ]

        with patch("coding_agents.resume.get_agent", return_value=mock_agent):
            cmd = prepare_resume_command(
                session, AgentType.CODEX, ExecutionConfig(), last_seq=42
            )

        assert cmd == [
            "codex", "exec", "do stuff", "--resume-from", "42"
        ]


# ---------------------------------------------------------------------------
# resume_session
# ---------------------------------------------------------------------------

class TestResumeSession:
    async def test_resume_creates_new_session(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        # Set up original session
        original = _make_session(
            session_id="orig-session",
            status=SessionStatus.COMPLETED,
            exit_code=0,
            prompt="continue this",
        )
        await storage.create_session(original)
        events = _make_events("orig-session", count=5)
        await storage.append_events(events)

        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "continue this"]

        # Mock executor
        mock_executor_instance = MagicMock()
        resume_events = _make_events("new-session", count=3)

        async def mock_execute(
            session_id: str,
            command: list[str],
            workdir: str,
            env: Optional[dict[str, str]] = None,
        ) -> AsyncIterator[Event]:
            for event in resume_events:
                yield event
            await storage.update_session(
                session_id,
                status=SessionStatus.COMPLETED,
                exit_code=0,
                finished_at=datetime.now(timezone.utc),
            )

        mock_executor_instance.execute = mock_execute

        with (
            patch("coding_agents.resume.get_agent", return_value=mock_agent),
            patch(
                "coding_agents.resume.StreamExecutor",
                return_value=mock_executor_instance,
            ),
        ):
            new_sid, collected = await resume_session(
                "orig-session", storage
            )

        # New session was created
        assert new_sid != "orig-session"
        new_session = await storage.get_session(new_sid)
        assert new_session is not None
        assert new_session.metadata.get("resumed_from") == "orig-session"
        assert new_session.metadata.get("resume_from_seq") == 5

        # Events collected
        assert len(collected) == 3

    async def test_resume_not_supported_raises(
        self, storage: SQLiteStorage
    ):
        session = _make_session(
            session_id="failed-sess",
            status=SessionStatus.FAILED,
            exit_code=1,
        )
        await storage.create_session(session)
        await storage.append_events(_make_events(session.id))

        with pytest.raises(ResumeNotSupportedError, match="cannot be resumed"):
            await resume_session(session.id, storage)

    async def test_resume_with_explicit_new_id(
        self, storage: SQLiteStorage
    ):
        original = _make_session(
            session_id="orig-2",
            status=SessionStatus.KILLED,
        )
        await storage.create_session(original)
        await storage.append_events(_make_events("orig-2", count=3))

        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "x"]

        mock_executor_instance = MagicMock()

        async def mock_execute(
            session_id: str,
            command: list[str],
            workdir: str,
            env: Optional[dict[str, str]] = None,
        ) -> AsyncIterator[Event]:
            yield Event(
                session_id=session_id,
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
            await storage.update_session(
                session_id,
                status=SessionStatus.COMPLETED,
                exit_code=0,
                finished_at=datetime.now(timezone.utc),
            )

        mock_executor_instance.execute = mock_execute

        with (
            patch("coding_agents.resume.get_agent", return_value=mock_agent),
            patch(
                "coding_agents.resume.StreamExecutor",
                return_value=mock_executor_instance,
            ),
        ):
            new_sid, _ = await resume_session(
                "orig-2", storage, new_session_id="my-custom-id"
            )

        assert new_sid == "my-custom-id"

    async def test_resume_command_includes_resume_flag(
        self, storage: SQLiteStorage
    ):
        """Verify the resume command includes --resume with session ID."""
        original = _make_session(
            session_id="flag-test",
            status=SessionStatus.COMPLETED,
            exit_code=0,
            prompt="test prompt",
        )
        await storage.create_session(original)
        await storage.append_events(_make_events("flag-test", count=3))

        captured_commands: list[list[str]] = []
        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "test prompt"]

        mock_executor_instance = MagicMock()

        async def mock_execute(
            session_id: str,
            command: list[str],
            workdir: str,
            env: Optional[dict[str, str]] = None,
        ) -> AsyncIterator[Event]:
            captured_commands.append(command)
            yield Event(
                session_id=session_id,
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
            await storage.update_session(
                session_id,
                status=SessionStatus.COMPLETED,
                exit_code=0,
                finished_at=datetime.now(timezone.utc),
            )

        mock_executor_instance.execute = mock_execute

        with (
            patch("coding_agents.resume.get_agent", return_value=mock_agent),
            patch(
                "coding_agents.resume.StreamExecutor",
                return_value=mock_executor_instance,
            ),
        ):
            await resume_session("flag-test", storage)

        assert len(captured_commands) == 1
        cmd = captured_commands[0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "flag-test"

    async def test_resume_seq_starts_from_last(
        self, storage: SQLiteStorage
    ):
        """The resume_from_seq in metadata should match the last event seq."""
        original = _make_session(
            session_id="seq-test",
            status=SessionStatus.TIMEOUT,
        )
        await storage.create_session(original)
        events = _make_events("seq-test", count=7)
        await storage.append_events(events)

        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "x"]

        mock_executor_instance = MagicMock()

        async def mock_execute(
            session_id: str,
            command: list[str],
            workdir: str,
            env: Optional[dict[str, str]] = None,
        ) -> AsyncIterator[Event]:
            yield Event(
                session_id=session_id,
                channel="system",
                seq=1,
                type=EventType.RESULT,
                data=json.dumps({"exit_code": 0}),
            )
            await storage.update_session(
                session_id,
                status=SessionStatus.COMPLETED,
                exit_code=0,
                finished_at=datetime.now(timezone.utc),
            )

        mock_executor_instance.execute = mock_execute

        with (
            patch("coding_agents.resume.get_agent", return_value=mock_agent),
            patch(
                "coding_agents.resume.StreamExecutor",
                return_value=mock_executor_instance,
            ),
        ):
            new_sid, _ = await resume_session("seq-test", storage)

        new_session = await storage.get_session(new_sid)
        assert new_session is not None
        assert new_session.metadata["resume_from_seq"] == 7


# ---------------------------------------------------------------------------
# enable_resume_support (cli_integration)
# ---------------------------------------------------------------------------

class TestEnableResumeSupport:
    def test_monkey_patch_adds_resume_flag(self):
        from coding_agents.orchestrator.cli_integration import (
            enable_resume_support,
        )

        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "hello"]

        enable_resume_support(mock_agent)

        # Without resume metadata → normal command
        config = ExecutionConfig()
        cmd = mock_agent.build_command("hello", config)
        assert cmd == ["claude", "-p", "hello"]

    def test_monkey_patch_with_resume_metadata(self):
        from coding_agents.orchestrator.cli_integration import (
            enable_resume_support,
        )

        mock_agent = MagicMock()
        mock_agent.build_command.return_value = ["claude", "-p", "hello"]

        enable_resume_support(mock_agent)

        # With resume metadata → command includes --resume
        config = ExecutionConfig()
        # Attach metadata to config (not part of the normal dataclass)
        config.metadata = {  # type: ignore[attr-defined]
            "resume": {"session_id": "sess-123", "last_seq": 10}
        }
        cmd = mock_agent.build_command("hello", config)
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-123"
