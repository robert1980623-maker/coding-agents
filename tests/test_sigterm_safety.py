"""Test signal handling safety for dispatch (v0.2.11).

Verifies that when the dispatch wrapper receives SIGTERM/SIGINT,
the session is properly marked as failed rather than left in running state.
"""
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coding_agents.models import SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


def test_sigterm_marks_session_failed(tmp_path: Path):
    """If dispatch receives SIGTERM mid-execution, the session must be FAILED.

    Before the fix, the wrapper exited without updating the session,
    leaving status=running until gc/recover turned it into orphaned.
    """
    db_path = tmp_path / "test.db"

    # Run dispatch in a subprocess with a fake adapter that sleeps
    script = f"""
import sys
import time
sys.path.insert(0, '{Path(__file__).parent.parent / "src"}')

from unittest.mock import patch
from coding_agents.cli import app
from coding_agents.models import AgentType

class FakeAdapter:
    def build_command(self, prompt, config):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
    import os
    os.environ["CODING_AGENTS_DB"] = "{db_path}"
    app(["dispatch", "claude", "fix the auth bug"])
"""

    # Start subprocess
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for session to be created and signal handler registered
    time.sleep(2)

    # Send SIGTERM
    proc.send_signal(signal.SIGTERM)

    # Wait for process to exit
    stdout, stderr = proc.communicate(timeout=5)

    # POSIX exit code: 128 + SIGTERM(15) = 143
    assert proc.returncode == 128 + signal.SIGTERM, (
        f"expected exit code {128 + signal.SIGTERM}, got {proc.returncode}\n"
        f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
    )

    # Check the DB: session must be FAILED (not stuck in RUNNING)
    async def check():
        store = SQLiteStorage(str(db_path))
        await store.initialize()
        try:
            sessions = await store.list_sessions()
            assert len(sessions) == 1, f"expected 1 session, got {len(sessions)}"
            s = sessions[0]

            # Must NOT be running / orphaned
            assert s.status == SessionStatus.FAILED, (
                f"expected FAILED, got {s.status.value}"
            )
            # Must have a finished_at timestamp
            assert s.finished_at is not None, "finished_at should be set"
            # exit_code: partial (unknown) → -1 by convention
            assert s.exit_code == -1, f"expected exit_code=-1, got {s.exit_code}"
            # Metadata must record the signal
            assert s.metadata is not None
            assert s.metadata.get("signal") == signal.SIGTERM
            assert s.metadata.get("error") == "wrapper terminated"
            assert "SIGTERM" in (s.metadata.get("signal_name") or "")
        finally:
            await store.close()

    asyncio.run(check())


def test_sigint_marks_session_failed(tmp_path: Path):
    """Same as SIGTERM but for Ctrl-C (SIGINT)."""
    db_path = tmp_path / "test.db"

    script = f"""
import sys
sys.path.insert(0, '{Path(__file__).parent.parent / "src"}')

from unittest.mock import patch
from coding_agents.cli import app

class FakeAdapter:
    def build_command(self, prompt, config):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
    import os
    os.environ["CODING_AGENTS_DB"] = "{db_path}"
    app(["dispatch", "claude", "fix the auth bug"])
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    time.sleep(2)
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=5)

    assert proc.returncode == 128 + signal.SIGINT

    async def check():
        store = SQLiteStorage(str(db_path))
        await store.initialize()
        try:
            sessions = await store.list_sessions()
            assert len(sessions) == 1
            s = sessions[0]
            assert s.status == SessionStatus.FAILED
            assert s.finished_at is not None
            assert s.metadata is not None
            assert s.metadata.get("signal") == signal.SIGINT
            assert "SIGINT" in (s.metadata.get("signal_name") or "")
        finally:
            await store.close()

    asyncio.run(check())


def test_normal_completion_not_affected(tmp_path: Path):
    """Normal completion should still mark session COMPLETED, not FAILED.

    Regression guard: the finally block must not stomp on a session
    that was already finalized by the executor's RESULT event.
    """
    db_path = tmp_path / "test.db"

    script = f"""
import sys
sys.path.insert(0, '{Path(__file__).parent.parent / "src"}')

from unittest.mock import patch
from coding_agents.cli import app

class FakeAdapter:
    def build_command(self, prompt, config):
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
    import os
    os.environ["CODING_AGENTS_DB"] = "{db_path}"
    app(["dispatch", "claude", "trivial task"])
"""

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0

    async def check():
        store = SQLiteStorage(str(db_path))
        await store.initialize()
        try:
            sessions = await store.list_sessions()
            assert len(sessions) == 1
            s = sessions[0]
            # The executor marks it COMPLETED via the RESULT event
            assert s.status != SessionStatus.FAILED, (
                f"normal completion should not be FAILED, got {s.status.value}"
            )
            assert s.status != SessionStatus.RUNNING, (
                "session must not be left in running"
            )
        finally:
            await store.close()

    asyncio.run(check())
