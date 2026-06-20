"""Tests for StreamExecutor."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coding_agents.executor import SeqCounter, StreamExecutor
from coding_agents.models import (
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.storage.sqlite import SQLiteStorage


class TestSeqCounter:
    async def test_monotonic(self):
        c = SeqCounter()
        v1 = await c.next()
        v2 = await c.next()
        v3 = await c.next()
        assert v1 == 1
        assert v2 == 2
        assert v3 == 3

    async def test_concurrent(self):
        c = SeqCounter()
        values = await asyncio.gather(*[c.next() for _ in range(10)])
        assert sorted(values) == list(range(1, 11))

    async def test_value_property(self):
        c = SeqCounter()
        assert c.value == 0
        await c.next()
        assert c.value == 1


class TestStreamExecutorSubprocess:
    """Test executor with a real subprocess (uses /bin/echo or python -c)."""

    async def test_execute_success(self, storage: SQLiteStorage, tmp_path: Path):
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Use a simple command that prints to stdout
        command = [sys.executable, "-c", "print('hello world')"]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        # Should have: session.start, stdout, result
        types = [e.type for e in events]
        assert EventType.SESSION_START in types
        assert EventType.STDOUT in types
        assert EventType.RESULT in types

        # Find stdout event
        stdout_event = next(e for e in events if e.type == EventType.STDOUT)
        assert "hello world" in stdout_event.data

        # Check session status updated
        loaded = await storage.get_session(session.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.COMPLETED
        assert loaded.exit_code == 0

    async def test_execute_failure(self, storage: SQLiteStorage, tmp_path: Path):
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Command that exits with non-zero
        command = [sys.executable, "-c", "import sys; sys.exit(42)"]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        loaded = await storage.get_session(session.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.FAILED
        assert loaded.exit_code == 42

    async def test_execute_subprocess_not_found(self, storage: SQLiteStorage, tmp_path: Path):
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Nonexistent command
        command = ["this_command_does_not_exist_abc123"]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        # Should have session.start + error
        types = [e.type for e in events]
        assert EventType.SESSION_START in types
        assert EventType.ERROR in types

        error_event = next(e for e in events if e.type == EventType.ERROR)
        data = json.loads(error_event.data)
        assert data["code"] == "SUBPROCESS_FAILED"

        # P0-NEW-5: session must be FAILED, not stuck in PENDING
        loaded = await storage.get_session(session.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.FAILED

    async def test_stderr_events(self, storage: SQLiteStorage, tmp_path: Path):
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Command that writes to both stdout and stderr
        command = [
            sys.executable, "-c",
            "import sys; print('out'); print('err', file=sys.stderr)"
        ]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        channels = {e.type for e in events}
        assert EventType.STDOUT in channels
        assert EventType.STDERR in channels

        stderr_event = next(e for e in events if e.type == EventType.STDERR)
        assert "err" in stderr_event.data

    async def test_seq_monotonic(self, storage: SQLiteStorage, tmp_path: Path):
        """All events should have globally monotonic seq."""
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        command = [
            sys.executable, "-c",
            "import sys; print('a'); print('b', file=sys.stderr); print('c')"
        ]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))

    async def test_events_stored(self, storage: SQLiteStorage, tmp_path: Path):
        """Events should be persisted to storage."""
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        command = [sys.executable, "-c", "print('stored')"]
        async for _ in executor.execute(session.id, command, str(tmp_path)):
            pass

        stored = await storage.get_events(session.id)
        assert len(stored) >= 2  # at least session.start + stdout


class TestStreamExecutorWatchdog:
    async def test_idle_timeout(self, storage: SQLiteStorage, tmp_path: Path):
        """Process killed after idle timeout."""
        config = ExecutionConfig(idle_timeout_seconds=1)
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Long-running process that never outputs
        command = [sys.executable, "-c", "import time; time.sleep(30)"]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)
            # Break early after getting enough events (the watchdog will terminate)

        loaded = await storage.get_session(session.id)
        assert loaded is not None
        assert loaded.status == SessionStatus.TIMEOUT


class TestStreamExecutorWatchPattern:
    async def test_watch_stop(self, storage: SQLiteStorage, tmp_path: Path):
        """Watch pattern with action='stop' terminates the process."""
        config = ExecutionConfig(
            watch_patterns=[
                {"pattern": "STOP_NOW", "action": "stop"}
            ]
        )
        # Use ExecutionConfig but need proper WatchPattern
        from coding_agents.models import WatchPattern
        config.watch_patterns = [WatchPattern(pattern="STOP_NOW", action="stop")]
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        command = [
            sys.executable, "-c",
            "print('hello'); print('STOP_NOW'); import time; time.sleep(30)"
        ]

        events = []
        async for event in executor.execute(session.id, command, str(tmp_path)):
            events.append(event)

        loaded = await storage.get_session(session.id)
        assert loaded is not None
        # Process should be terminated (exit_code might be -15 for SIGTERM)
        assert loaded.status in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.KILLED}


class TestExtractText:
    def test_passthrough(self):
        config = ExecutionConfig(output_mode="passthrough")
        executor = StreamExecutor(store=MagicMock(), config=config)
        assert executor._extract_text("hello", "passthrough") == "hello"

    def test_standard_non_json(self):
        config = ExecutionConfig()
        executor = StreamExecutor(store=MagicMock(), config=config)
        assert executor._extract_text("plain text", "standard") == "plain text"

    def test_standard_claude_assistant(self):
        config = ExecutionConfig()
        executor = StreamExecutor(store=MagicMock(), config=config)
        line = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello!"}]}
        })
        assert executor._extract_text(line, "standard") == "Hello!"

    def test_standard_codex_item(self):
        config = ExecutionConfig()
        executor = StreamExecutor(store=MagicMock(), config=config)
        line = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "World"}
        })
        assert executor._extract_text(line, "standard") == "World"


class TestFlush:
    async def test_atomic_flush(self, storage: SQLiteStorage):
        """_flush should atomically swap the buffer."""
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir="/tmp")
        await storage.create_session(session)

        # Add events to buffer
        for i in range(5):
            executor._buffer.append(Event(
                session_id=session.id,
                channel="stdout",
                seq=i + 1,
                type=EventType.STDOUT,
                data=f"line{i}",
            ))

        assert len(executor._buffer) == 5
        await executor._flush()
        assert len(executor._buffer) == 0

        # Events should be stored
        stored = await storage.get_events(session.id)
        assert len(stored) == 5

    async def test_flush_empty(self, storage: SQLiteStorage):
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)
        # Should be a no-op
        await executor._flush()
        assert executor._buffer == []


class TestSubprocessProcessGroup:
    """v0.2.12: subprocess must run in its own process group so wrapper
    SIGTERM/SIGKILL doesn't propagate to it."""

    async def test_subprocess_runs_in_new_session(
        self, storage: SQLiteStorage, tmp_path: Path
    ):
        """The spawned subprocess should have a different session ID
        (process group) from the wrapper."""
        import os
        config = ExecutionConfig()
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        # Command that prints its own session ID as JSON
        command = [
            sys.executable, "-c",
            "import os, json; print(json.dumps({'sid': os.getsid(0)}))",
        ]

        wrapper_sid = os.getsid(os.getpid())
        child_sid = None
        import json as _json
        async for event in executor.execute(session.id, command, str(tmp_path)):
            if event.type == EventType.STDOUT:
                child_sid = _json.loads(event.data.strip())["sid"]

        assert child_sid is not None, "subprocess didn't print its session ID"
        assert child_sid != wrapper_sid, (
            f"subprocess should be in a new session "
            f"(wrapper sid={wrapper_sid}, child sid={child_sid})"
        )
