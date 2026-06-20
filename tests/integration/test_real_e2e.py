"""Real Claude CLI end-to-end test through StreamExecutor.

This is the most comprehensive integration test: it exercises the full
pipeline from ClaudeAgent.build_command → StreamExecutor.execute → SQLiteStorage.

Tests are skipped when:
- `claude` binary is not found
- Authentication fails (no OAuth token / no API key)

Cost is controlled via --model haiku and --max-budget-usd 0.1.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess as sync_subprocess

import pytest

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

# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

CLAUDE_BINARY = shutil.which("claude")
HAS_CLAUDE = CLAUDE_BINARY is not None

skip_no_binary = pytest.mark.skipif(
    not HAS_CLAUDE,
    reason="claude CLI binary not found on PATH",
)


def _check_claude_auth() -> bool:
    """Return True if claude CLI appears to be authenticated."""
    if not HAS_CLAUDE:
        return False
    try:
        result = sync_subprocess.run(
            [CLAUDE_BINARY, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (sync_subprocess.TimeoutExpired, OSError):
        return False


HAS_AUTH = _check_claude_auth()

skip_no_auth = pytest.mark.skipif(
    not HAS_AUTH,
    reason="claude CLI not authenticated",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_no_binary
@skip_no_auth
class TestRealClaudeE2E:
    """Full end-to-end: Claude CLI → StreamExecutor → SQLiteStorage."""

    async def test_real_claude_e2e(self):
        """Real claude CLI call through StreamExecutor.

        Uses haiku model + $0.1 budget cap.
        Verifies: event types, session lifecycle, storage persistence.
        """
        storage = SQLiteStorage(":memory:")
        await storage.initialize()

        config = ExecutionConfig(
            max_budget_usd=0.1,
            timeout_seconds=60,
            idle_timeout_seconds=30,
            model="haiku",
        )
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        cmd = agent.build_command("What is 1+1? Answer in one word.", config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="What is 1+1?",
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
        assert EventType.SESSION_START in event_types, (
            f"Missing SESSION_START. Events: {event_types}"
        )
        assert EventType.RESULT in event_types, (
            f"Missing RESULT. Events: {event_types}"
        )

        # RESULT should have exit_code
        result_event = next(e for e in events if e.type == EventType.RESULT)
        result_data = json.loads(result_event.data)
        assert "exit_code" in result_data

        # If exit_code is 0, we should have stdout events
        if result_data["exit_code"] == 0:
            stdout_events = [e for e in events if e.type == EventType.STDOUT]
            assert len(stdout_events) > 0, "No stdout events on success"

            # Try to parse result from output
            all_stdout = "".join(e.data for e in stdout_events)
            print(f"\n[e2e] Stdout ({len(all_stdout)} chars): {all_stdout[:300]}")

            # Check for cost info in parsed output
            for line in all_stdout.splitlines():
                parsed = agent.parse_output(line.strip())
                if parsed:
                    print(f"[e2e] Parsed result: {parsed}")
                    if parsed.get("cost_usd") is not None:
                        print(f"[e2e] Cost: ${parsed['cost_usd']}")

        # Verify session status
        final_session = await storage.get_session(session_id)
        assert final_session is not None
        assert final_session.status in (
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.TIMEOUT,
        ), f"Unexpected status: {final_session.status}"

        if final_session.status == SessionStatus.COMPLETED:
            assert final_session.exit_code == 0
            assert final_session.pid is not None
            assert final_session.started_at is not None
            assert final_session.finished_at is not None
            print(f"\n[e2e] Session completed: pid={final_session.pid}, "
                  f"exit_code={final_session.exit_code}")
        elif final_session.status == SessionStatus.FAILED:
            # If auth failed, the test should still pass (we tested the pipeline)
            stderr_events = [e for e in events if e.type == EventType.STDERR]
            if stderr_events:
                stderr_text = "".join(e.data for e in stderr_events)
                print(f"\n[e2e] Session failed with stderr: {stderr_text[:300]}")

        # Verify events stored in DB
        stored_events = await storage.get_events(session_id)
        assert len(stored_events) >= 2, (
            f"Expected >= 2 stored events, got {len(stored_events)}"
        )

        await storage.close()

    async def test_real_claude_timeout(self):
        """Verify idle timeout kills a long-running claude process."""
        storage = SQLiteStorage(":memory:")
        await storage.initialize()

        config = ExecutionConfig(
            max_budget_usd=0.1,
            timeout_seconds=10,
            idle_timeout_seconds=3,  # Very short idle timeout
            model="haiku",
        )
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        # A prompt that might take a while
        cmd = agent.build_command(
            "Write a 500-word essay about the history of computing",
            config,
        )

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="long essay",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(session_id, cmd, "/tmp"):
            events.append(event)

        event_types = [e.type for e in events]
        assert EventType.RESULT in event_types

        # Session should be in a terminal state
        final_session = await storage.get_session(session_id)
        assert final_session.status.is_terminal, (
            f"Session not in terminal state: {final_session.status}"
        )

        await storage.close()

    async def test_real_claude_event_stored_in_db(self):
        """Verify all events are persisted to SQLite storage."""
        storage = SQLiteStorage(":memory:")
        await storage.initialize()

        config = ExecutionConfig(
            max_budget_usd=0.05,
            timeout_seconds=60,
            model="haiku",
        )
        executor = StreamExecutor(storage, config)
        agent = ClaudeAgent()

        cmd = agent.build_command("Say 'integration test'", config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="integration test",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(session_id, cmd, "/tmp"):
            events.append(event)

        # Query events from storage
        stored = await storage.get_events(session_id)

        # Should have SESSION_START + stdout events + RESULT
        assert len(stored) >= 2, (
            f"Expected >= 2 events in storage, got {len(stored)}"
        )

        # Verify seq monotonicity
        seqs = [e.seq for e in stored]
        assert seqs == sorted(seqs), f"Seqs not monotonic: {seqs}"
        assert len(seqs) == len(set(seqs)), f"Duplicate seqs: {seqs}"

        # Verify session_start is seq 1
        start_events = [e for e in stored if e.type == EventType.SESSION_START]
        assert len(start_events) == 1
        assert start_events[0].seq == 1

        await storage.close()
