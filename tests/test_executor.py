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
        """Watch pattern with action='stop' terminates the process.

        The subprocess prints 'hello' then 'STOP_NOW' then sleeps for a
        long time. The watch pattern fires on 'STOP_NOW' and calls
        ``executor._process.terminate()`` to kill the process group.

        Without an outer ``wait_for`` guard, the test's ``async for`` would
        wait forever for the executor to finish (subprocess sleep blocks
        the stdout reader). The 5-second guard ensures a regression that
        doesn't actually stop the process is caught quickly with a clear
        error instead of a 30-second hang.
        """
        from coding_agents.models import WatchPattern
        config = ExecutionConfig(
            watch_patterns=[WatchPattern(pattern="STOP_NOW", action="stop")],
        )
        executor = StreamExecutor(store=storage, config=config)

        session = Session(agent="claude", prompt="test", workdir=str(tmp_path))
        await storage.create_session(session)

        command = [
            sys.executable, "-u", "-c",
            "print('hello'); print('STOP_NOW'); import time; time.sleep(30)",
        ]

        events: list[Event] = []

        async def _collect_all() -> list[Event]:
            # v0.2.14: collect ALL events until the executor's stream
            # terminates. The stream ends when the readers see EOF on
            # the subprocess's stdout/stderr pipes (which happens when
            # the watch stop action kills the subprocess). The result
            # event is yielded last.
            async for event in executor.execute(session.id, command, str(tmp_path)):
                events.append(event)
            return events

        # Hard timeout: if watch stop doesn't actually terminate the
        # subprocess, the test fails fast instead of waiting 30s.
        try:
            events = await asyncio.wait_for(_collect_all(), timeout=5.0)
        except asyncio.TimeoutError:
            # Defensive cleanup so the test process doesn't leak the
            # detached subprocess (v0.2.14 leaves it running).
            if executor._process is not None and executor._process.returncode is None:
                try:
                    import signal as _sig
                    import os as _os
                    _os.killpg(_os.getpgid(executor._process.pid), _sig.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            pytest.fail(
                "executor.execute() did not exit within 5s after watch "
                "pattern fired. watch stop action is not terminating the "
                "subprocess."
            )

        # Sanity: we received the result event (terminal).
        result_events = [e for e in events if e.type == EventType.RESULT]
        assert len(result_events) == 1, (
            f"expected exactly one RESULT event, got {len(result_events)}: "
            f"{[e.type.value for e in events]}"
        )

        loaded = await storage.get_session(session.id)
        assert loaded is not None
        # The watch pattern fired terminate() on the process group, so
        # the subprocess is killed and the executor finalizes the session
        # as FAILED with exit_code=-15 (killed by SIGTERM).
        assert loaded.status in {SessionStatus.COMPLETED, SessionStatus.FAILED}, (
            f"expected terminal status, got {loaded.status.value}"
        )
        assert loaded.exit_code is not None
        assert loaded.exit_code < 0, (
            f"expected negative exit code (killed by signal), got {loaded.exit_code}"
        )


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
    """v0.2.14: subprocess must run in its own process group so wrapper
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


def test_subprocess_detached_process_group(tmp_path: Path):
    """v0.2.14: When the wrapper receives SIGTERM, the spawned subprocess
    must CONTINUE running (because start_new_session=True detaches it into
    its own process group), and the session must be marked FAILED.

    This is the "active detach" design from the architect feedback: wrapper
    death (e.g. OpenClaw 1MB-buffer SIGKILL cascade) does NOT cascade to
    the agent subprocess. The user can still ``tail`` the agent's output
    after the wrapper is gone.

    Subprocess-based: we spawn the wrapper as a real OS process, send it
    SIGTERM, then check ``ps`` for the child and the DB for the session
    status. In-process tests cannot reproduce the OS-level process group
    semantics.

    The child PID is read from the SQLite session row (the executor
    stores ``pid`` on the session after subprocess launch). This avoids
    the v0.2.6 bounded-output contract, which keeps intermediate agent
    output out of the wrapper's stdout.
    """
    import os
    import signal
    import subprocess
    import sys as _sys
    import time
    from pathlib import Path as _Path

    from coding_agents.models import SessionStatus
    from coding_agents.storage.sqlite import SQLiteStorage

    db_path = tmp_path / "test.db"

    # The fake agent spawns a long-running Python subprocess that just
    # sleeps. The wrapper will receive SIGTERM while it is sleeping,
    # so we can verify the subprocess is detached (still alive) after
    # the wrapper dies.
    script = f"""
import sys, os
sys.path.insert(0, '{_Path(__file__).parent.parent / "src"}')

from unittest.mock import patch
from coding_agents.cli import app

class FakeAdapter:
    def build_command(self, prompt, config):
        return [
            {_sys.executable!r}, "-c",
            "import time; time.sleep(120)",
        ]

with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
    os.environ["CODING_AGENTS_DB"] = {str(db_path)!r}
    app(["dispatch", "claude", "test detached subprocess"])
"""

    # Start the wrapper as a real OS process.
    proc = subprocess.Popen(
        [_sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    child_pid = None
    child_pgid = None
    try:
        # Wait for the session row to be populated with the subprocess
        # PID. The executor sets session.pid right after create_subprocess_exec.
        async def _read_child_pid():
            store = SQLiteStorage(str(db_path))
            await store.initialize()
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    sessions = await store.list_sessions()
                    if sessions and sessions[0].pid is not None:
                        return int(sessions[0].pid)
                    await asyncio.sleep(0.05)
                return None
            finally:
                await store.close()

        child_pid = asyncio.run(_read_child_pid())
        assert child_pid is not None, (
            f"subprocess PID was never recorded in the session row within 10s. "
            f"This means the executor never reached the RUNNING state, or "
            f"the database is in an unexpected state."
        )
        child_pgid = os.getpgid(child_pid)

        # Sanity check: the subprocess is alive before we send SIGTERM.
        try:
            os.kill(child_pid, 0)
            child_alive_before = True
        except ProcessLookupError:
            child_alive_before = False
        assert child_alive_before, (
            f"subprocess pid={child_pid} is already dead before we even "
            f"sent SIGTERM to the wrapper"
        )

        # v0.2.14 invariant: child is in a different process group
        # from the wrapper, so wrapper SIGTERM does not propagate.
        assert child_pgid != os.getpgid(0), (
            f"subprocess is in the same process group as the test "
            f"(pgid={child_pgid}); start_new_session=True is not in effect"
        )

        # Send SIGTERM to the wrapper.
        proc.send_signal(signal.SIGTERM)

        # Wait for the wrapper to exit. v0.2.14's signal handler
        # converts SIGTERM into SystemExit, the executor's finally
        # block uses a 0.5s wait timeout for the detached subprocess,
        # the dispatch finally block finalizes the session as FAILED.
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail(
                "wrapper did not exit within 10s after SIGTERM. "
                "v0.2.14's executor should not block on the detached subprocess."
            )

        # POSIX exit code: 128 + SIGTERM(15) = 143
        assert proc.returncode == 128 + signal.SIGTERM, (
            f"expected exit code {128 + signal.SIGTERM} (POSIX), got {proc.returncode}"
        )

        # The detached subprocess MUST still be alive: the wrapper's
        # SIGTERM was not propagated because start_new_session=True
        # put the child in its own process group (active detach).
        try:
            os.kill(child_pid, 0)
            child_alive_after = True
        except ProcessLookupError:
            child_alive_after = False
        assert child_alive_after, (
            f"subprocess pid={child_pid} was killed when the wrapper "
            f"died -- v0.2.14's start_new_session=True should have "
            f"detached it into its own process group. This is a "
            f"regression of the active-detach design."
        )

        # The session must be marked FAILED (v0.2.11 signal handler
        # contract that v0.2.14 inherits and extends).
        async def _check_session():
            store = SQLiteStorage(str(db_path))
            await store.initialize()
            try:
                sessions = await store.list_sessions()
                assert len(sessions) == 1, (
                    f"expected 1 session, got {len(sessions)}"
                )
                s = sessions[0]
                assert s.status == SessionStatus.FAILED, (
                    f"expected FAILED (v0.2.11 signal handler), got {s.status.value}"
                )
                assert s.metadata is not None
                assert s.metadata.get("signal") == signal.SIGTERM, (
                    f"expected signal metadata SIGTERM, got {s.metadata!r}"
                )
            finally:
                await store.close()

        asyncio.run(_check_session())

    finally:
        # Cleanup: kill the entire detached process group (we own the
        # subprocess as a side-effect of spawning it). Best-effort and
        # never raises -- pytest cleanup must be idempotent.
        if child_pgid is not None:
            try:
                os.killpg(child_pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
