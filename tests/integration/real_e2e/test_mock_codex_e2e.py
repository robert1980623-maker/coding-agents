"""Mock Codex CLI E2E tests.

Tests the full pipeline: CodexAgent.build_command → StreamExecutor.execute → parse_output
with mock codex binary (no API key needed).
"""

from __future__ import annotations

import json
from pathlib import Path

from coding_agents.agents.codex import CodexAgent
from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


class TestMockCodexE2E:
    """Full E2E with mock codex CLI (no API key required)."""

    async def test_mock_codex_event_parsing(self, tmp_path: Path, mock_cli_env: dict):
        """Verify event parsing through the full pipeline."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(
            timeout_seconds=30,
            idle_timeout_seconds=15,
        )
        executor = StreamExecutor(storage, config)
        agent = CodexAgent()

        # Build command (will use mock codex from PATH)
        cmd = agent.build_command("test prompt", config)

        session = Session(
            agent=AgentType.CODEX,
            prompt="test prompt",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(session_id, cmd, "/tmp"):
            events.append(event)
            if event.type == EventType.RESULT:
                break

        # Verify event lifecycle
        event_types = [e.type for e in events]
        assert EventType.SESSION_START in event_types
        assert EventType.STDOUT in event_types
        assert EventType.RESULT in event_types

        # Verify stdout events can be parsed
        stdout_events = [e for e in events if e.type == EventType.STDOUT]
        assert len(stdout_events) > 0

        # Parse each line and check for usage info
        parsed_results = []
        for event in stdout_events:
            for line in event.data.splitlines():
                parsed = agent.parse_output(line.strip())
                if parsed:
                    parsed_results.append(parsed)

        # Should have parsed at least one result with usage info
        assert len(parsed_results) > 0, "No result events parsed"

        # Check token extraction (Codex doesn't provide cost)
        result = parsed_results[-1]
        assert "input_tokens" in result
        assert result["input_tokens"] == 200
        assert "output_tokens" in result
        assert result["output_tokens"] == 30

        await storage.close()

    async def test_mock_codex_no_cost_info(self, tmp_path: Path, mock_cli_env: dict):
        """Verify Codex doesn't provide cost (by design)."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)
        agent = CodexAgent()

        cmd = agent.build_command("test", config)

        session = Session(agent=AgentType.CODEX, prompt="test", workdir="/tmp")
        session_id = await storage.create_session(session)

        # Collect all stdout
        all_output = ""
        async for event in executor.execute(session_id, cmd, "/tmp"):
            if event.type == EventType.STDOUT:
                all_output += event.data

        # Codex doesn't provide cost
        cost = agent.extract_cost(all_output)
        assert cost is None

        await storage.close()

    async def test_mock_codex_session_lifecycle(self, tmp_path: Path, mock_cli_env: dict):
        """Verify session status transitions."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)
        agent = CodexAgent()

        cmd = agent.build_command("test", config)

        session = Session(agent=AgentType.CODEX, prompt="test", workdir="/tmp")
        session_id = await storage.create_session(session)

        async for event in executor.execute(session_id, cmd, "/tmp"):
            if event.type == EventType.RESULT:
                break

        # Verify final session status
        final_session = await storage.get_session(session_id)
        assert final_session is not None
        assert final_session.status == SessionStatus.COMPLETED
        assert final_session.exit_code == 0
        assert final_session.pid is not None

        await storage.close()
