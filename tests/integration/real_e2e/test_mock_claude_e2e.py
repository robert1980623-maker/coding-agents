"""Mock Claude CLI E2E tests.

Tests the full pipeline: ClaudeAgent.build_command → StreamExecutor.execute → parse_output
with mock claude binary (no API key needed).
"""

from __future__ import annotations

import json
from pathlib import Path

from coding_agents.agents.claude import ClaudeAgent
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


class TestMockClaudeE2E:
    """Full E2E with mock claude CLI (no API key required)."""

    async def test_mock_claude_event_parsing(self, tmp_path: Path, mock_cli_env: dict):
        """Verify event parsing through the full pipeline."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(
            max_budget_usd=0.5,
            timeout_seconds=30,
            idle_timeout_seconds=15,
            model="haiku",
        )
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        # Build command (will use mock claude from PATH)
        cmd = agent.build_command("test prompt", config)

        session = Session(
            agent=AgentType.CLAUDE,
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

        # Parse each line and check for result event
        parsed_results = []
        for event in stdout_events:
            for line in event.data.splitlines():
                parsed = agent.parse_output(line.strip())
                if parsed:
                    parsed_results.append(parsed)

        # Should have parsed at least one result with cost info
        assert len(parsed_results) > 0, "No result events parsed"

        # Check cost extraction
        result = parsed_results[-1]
        assert "cost_usd" in result
        assert result["cost_usd"] is not None
        assert result["cost_usd"] == 0.00123

        # Check token extraction
        assert "input_tokens" in result
        assert result["input_tokens"] == 150
        assert "output_tokens" in result
        assert result["output_tokens"] == 25

        await storage.close()

    async def test_mock_claude_cost_extraction(self, tmp_path: Path, mock_cli_env: dict):
        """Verify cost extraction from result event."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        # Use passthrough mode to get raw JSON for cost extraction
        config = ExecutionConfig(timeout_seconds=30, output_mode="passthrough")
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        cmd = agent.build_command("test", config)

        session = Session(agent=AgentType.CLAUDE, prompt="test", workdir="/tmp")
        session_id = await storage.create_session(session)

        # Collect all stdout
        all_output = ""
        async for event in executor.execute(session_id, cmd, "/tmp"):
            if event.type == EventType.STDOUT:
                all_output += event.data

        # Extract cost
        cost = agent.extract_cost(all_output)
        assert cost is not None
        assert cost == 0.00123

        await storage.close()

    async def test_mock_claude_session_lifecycle(self, tmp_path: Path, mock_cli_env: dict):
        """Verify session status transitions."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        cmd = agent.build_command("test", config)

        session = Session(agent=AgentType.CLAUDE, prompt="test", workdir="/tmp")
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
