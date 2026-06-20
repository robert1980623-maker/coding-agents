"""Integration test: kill command terminates subprocess within 10s.

Tests the full chain:
  1. Start a long-running subprocess (sleep 60)
  2. Use CLI kill command to set DB status to KILLED
  3. Verify heartbeat checker picks up the signal and terminates the process
  4. Total time from kill to termination must be < 10s
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    AgentType,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


class TestKillRealProcess:
    """Test that the heartbeat checker actually kills the subprocess."""

    async def test_kill_terminates_sleep_60(self, storage: SQLiteStorage, tmp_path: Path):
        """Start `sleep 60`, then update DB to KILLED, verify process dies < 10s."""
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent=AgentType.CLAUDE, prompt="sleep test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Start the executor in a background task
        async def run_executor():
            events = []
            async for event in executor.execute(
                session.id, ["sleep", "60"], str(tmp_path)
            ):
                events.append(event)
            return events

        exec_task = asyncio.create_task(run_executor())

        # Wait until the process is running and has a PID
        for _ in range(30):
            await asyncio.sleep(0.1)
            s = await storage.get_session(session.id)
            if s is not None and s.status == SessionStatus.RUNNING and s.pid is not None:
                break
        else:
            pytest.fail("Process did not start within 3s")

        session_info = await storage.get_session(session.id)
        assert session_info is not None
        pid = session_info.pid
        assert pid is not None

        # Verify process is actually alive
        import os
        assert _pid_exists(pid), f"PID {pid} should be alive"

        # Now "kill" via DB update (simulates the CLI kill command)
        kill_start = time.monotonic()
        await storage.update_session(
            session.id,
            status=SessionStatus.KILLED,
            finished_at=None,  # Don't set finished_at here — executor will handle it
        )

        # Wait for executor to finish
        try:
            events = await asyncio.wait_for(exec_task, timeout=10.0)
        except asyncio.TimeoutError:
            exec_task.cancel()
            pytest.fail("Executor did not finish within 10s after kill signal")

        kill_duration = time.monotonic() - kill_start

        # Verify process is dead
        await asyncio.sleep(0.1)  # give OS a moment
        assert not _pid_exists(pid), f"PID {pid} should be dead after kill"

        # Kill should complete well within 10s (typical: ~2s for poll interval)
        assert kill_duration < 10.0, f"Kill took {kill_duration:.2f}s, expected < 10s"

        # Verify we got a result event
        result_events = [e for e in events if e.type == EventType.RESULT]
        assert len(result_events) >= 1

    async def test_kill_via_failed_status(self, storage: SQLiteStorage, tmp_path: Path):
        """FAILED status should also trigger termination."""
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent=AgentType.CLAUDE, prompt="fail test", workdir=str(tmp_path))
        await storage.create_session(session)

        async def run_executor():
            events = []
            async for event in executor.execute(
                session.id, ["sleep", "60"], str(tmp_path)
            ):
                events.append(event)
            return events

        exec_task = asyncio.create_task(run_executor())

        # Wait for running
        for _ in range(30):
            await asyncio.sleep(0.1)
            s = await storage.get_session(session.id)
            if s is not None and s.status == SessionStatus.RUNNING:
                break

        # Set FAILED
        await storage.update_session(session.id, status=SessionStatus.FAILED)

        # Should terminate quickly
        try:
            events = await asyncio.wait_for(exec_task, timeout=10.0)
        except asyncio.TimeoutError:
            exec_task.cancel()
            pytest.fail("Executor did not finish within 10s after FAILED signal")


def _pid_exists(pid: int) -> bool:
    """Check if a process with given PID exists."""
    import os
    import errno
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        # Permission denied means process exists
        return True
    return True
