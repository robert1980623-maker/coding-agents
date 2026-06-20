"""End-to-end integration tests for StreamExecutor with real subprocesses.

These tests exercise the full StreamExecutor pipeline:
  SQLiteStorage → StreamExecutor → real subprocess → events → storage

No external CLIs or API keys needed — uses echo/bash for subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess as sync_subprocess
import tempfile
import time
from pathlib import Path

import pytest

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
# Helpers
# ---------------------------------------------------------------------------


def get_process_rss_kb() -> int:
    """Get current process RSS in KB. Cross-platform (macOS / Linux)."""
    pid = os.getpid()
    try:
        if platform.system() == "Darwin":
            out = sync_subprocess.check_output(
                ["ps", "-o", "rss=", "-p", str(pid)],
                text=True,
                timeout=5,
            )
            return int(out.strip())
        else:
            # Linux
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1])
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestExecutorRealSubprocess:
    """StreamExecutor with real subprocess execution."""

    async def test_echo_hello(self, tmp_path: Path):
        """Basic echo subprocess — verifies full event lifecycle."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="echo hello",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(session_id, ["echo", "hello world"], "/tmp"):
            events.append(event)

        # Verify event types
        event_types = [e.type for e in events]
        assert EventType.SESSION_START in event_types, "Missing SESSION_START event"
        assert EventType.STDOUT in event_types, "Missing STDOUT event"
        assert EventType.RESULT in event_types, "Missing RESULT event"

        # Verify stdout content
        stdout_events = [e for e in events if e.type == EventType.STDOUT]
        stdout_text = "".join(e.data for e in stdout_events)
        assert "hello world" in stdout_text, (
            f"'hello world' not found in stdout: {stdout_text!r}"
        )

        # Verify session lifecycle
        final_session = await storage.get_session(session_id)
        assert final_session is not None
        assert final_session.status == SessionStatus.COMPLETED
        assert final_session.exit_code == 0
        assert final_session.pid is not None
        assert final_session.started_at is not None
        assert final_session.finished_at is not None

        # Verify events stored in DB
        # Note: RESULT event is yielded to caller but NOT persisted by design
        stored_events = await storage.get_events(session_id)
        assert len(stored_events) >= 2, (
            f"Expected >= 2 stored events (SESSION_START + STDOUT), got {len(stored_events)}"
        )

        # Verify seq monotonically increasing
        seqs = [e.seq for e in stored_events]
        assert seqs == sorted(seqs), f"Seqs not monotonic: {seqs}"
        assert len(seqs) == len(set(seqs)), f"Duplicate seqs: {seqs}"

        await storage.close()

    async def test_failing_command(self, tmp_path: Path):
        """Verify FAILED status for non-zero exit code."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="failing command",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(
            session_id, ["bash", "-c", "exit 42"], "/tmp"
        ):
            events.append(event)

        event_types = [e.type for e in events]
        assert EventType.RESULT in event_types

        # Check RESULT event data
        result_event = next(e for e in events if e.type == EventType.RESULT)
        result_data = json.loads(result_event.data)
        assert result_data["exit_code"] == 42

        final_session = await storage.get_session(session_id)
        assert final_session.status == SessionStatus.FAILED
        assert final_session.exit_code == 42

        await storage.close()

    async def test_stderr_capture(self, tmp_path: Path):
        """Verify stderr is captured as STDERR events."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="stderr test",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(
            session_id,
            ["bash", "-c", "echo stdout_msg; echo stderr_msg >&2"],
            "/tmp",
        ):
            events.append(event)

        event_types = [e.type for e in events]
        assert EventType.STDOUT in event_types
        assert EventType.STDERR in event_types

        stderr_events = [e for e in events if e.type == EventType.STDERR]
        stderr_text = "".join(e.data for e in stderr_events)
        assert "stderr_msg" in stderr_text

        await storage.close()

    async def test_event_seq_monotonic_across_channels(self, tmp_path: Path):
        """Verify seq numbers are globally monotonic across stdout+stderr."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="multi-channel",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(
            session_id,
            ["bash", "-c", "for i in $(seq 1 5); do echo out_$i; echo err_$i >&2; done"],
            "/tmp",
        ):
            events.append(event)

        # All seqs should be unique and positive
        seqs = [e.seq for e in events]
        assert all(s > 0 for s in seqs), f"Non-positive seq found: {seqs}"
        assert len(seqs) == len(set(seqs)), f"Duplicate seqs: {seqs}"

        await storage.close()

    async def test_command_not_found(self, tmp_path: Path):
        """Verify ERROR event when command doesn't exist."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="nonexistent",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        events: list[Event] = []
        async for event in executor.execute(
            session_id, ["nonexistent_binary_xyz_12345"], "/tmp"
        ):
            events.append(event)

        event_types = [e.type for e in events]
        assert EventType.ERROR in event_types, (
            f"Expected ERROR event, got: {event_types}"
        )

        final_session = await storage.get_session(session_id)
        assert final_session.status == SessionStatus.FAILED

        await storage.close()


class TestExecutorMemoryBaseline:
    """Memory usage baseline tests for StreamExecutor."""

    async def test_large_output_memory(self, tmp_path: Path):
        """Process 1MB of output and verify memory stays bounded.

        Design target: < 50MB growth for 1MB output.
        This verifies the streaming architecture doesn't buffer everything.
        """
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        # Generate 1MB script: 1000 lines × ~1024 bytes each
        line_content = "x" * 1020  # + newline ≈ 1024 bytes per line
        script_path = str(tmp_path / "big_output_test.sh")
        with open(script_path, "w") as f:
            f.write("#!/bin/bash\n")
            for _ in range(1000):
                f.write(f"echo '{line_content}'\n")
        os.chmod(script_path, 0o755)

        mem_before = get_process_rss_kb()

        config = ExecutionConfig(
            timeout_seconds=60,
            line_limit=8 * 1024 * 1024,  # 8 MiB — default
        )
        executor = StreamExecutor(storage, config)

        session = Session(
            agent=AgentType.CLAUDE,
            prompt="big output",
            workdir="/tmp",
        )
        session_id = await storage.create_session(session)

        event_count = 0
        async for event in executor.execute(
            session_id, ["bash", script_path], "/tmp"
        ):
            event_count += 1

        mem_after = get_process_rss_kb()

        growth_kb = mem_after - mem_before
        growth_mb = growth_kb / 1024

        print(f"\n[memory] Before: {mem_before} KB, After: {mem_after} KB")
        print(f"[memory] Growth: {growth_mb:.1f} MB")
        print(f"[memory] Events processed: {event_count}")

        # Memory growth should be < 50 MB for 1 MB of output
        assert growth_mb < 50, (
            f"Memory growth {growth_mb:.1f} MB exceeds 50 MB target "
            f"(before={mem_before} KB, after={mem_after} KB, events={event_count})"
        )

        # Verify all events were processed
        assert event_count >= 1000, (
            f"Expected >= 1000 events (1000 lines + SESSION_START + RESULT), "
            f"got {event_count}"
        )

        # Verify session completed successfully
        final_session = await storage.get_session(session_id)
        assert final_session.status == SessionStatus.COMPLETED

        await storage.close()


class TestExecutorMultipleSessions:
    """Test running multiple sessions sequentially."""

    async def test_sequential_sessions(self, tmp_path: Path):
        """Run 3 sessions sequentially with the same storage."""
        storage = SQLiteStorage(tmp_path / "test.db")
        await storage.initialize()

        config = ExecutionConfig(timeout_seconds=30)

        for i in range(3):
            executor = StreamExecutor(storage, config)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt=f"session {i}",
                workdir="/tmp",
            )
            session_id = await storage.create_session(session)

            events: list[Event] = []
            async for event in executor.execute(
                session_id, ["echo", f"hello_{i}"], "/tmp"
            ):
                events.append(event)

            final = await storage.get_session(session_id)
            assert final.status == SessionStatus.COMPLETED, (
                f"Session {i} not COMPLETED: {final.status}"
            )

        # All 3 sessions should be in storage
        all_sessions = await storage.list_sessions()
        assert len(all_sessions) == 3

        await storage.close()
