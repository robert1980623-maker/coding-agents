"""Tests for CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
def mock_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch DEFAULT_DB and CODING_AGENTS_DB env var to use a temp directory."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


class TestRunCommand:
    def test_run_echo(self, mock_db: Path):
        """Run a simple echo command as a fake agent."""
        # This test would require mocking the subprocess. Skip for now,
        # we test the executor separately.
        pass


class TestStatusCommand:
    def test_status_existing(self, mock_db: Path):
        """Show status of an existing session."""
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            session = Session(agent=AgentType.CLAUDE, prompt="test prompt", workdir="/tmp")
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["status", sid])
        assert result.exit_code == 0
        assert sid[:8] in result.stdout or sid in result.stdout

    def test_status_nonexistent(self, mock_db: Path):
        result = runner.invoke(app, ["status", "does-not-exist"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()


class TestListCommand:
    def test_list_empty(self, mock_db: Path):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "no sessions" in result.stdout.lower()

    def test_list_with_sessions(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            for i in range(3):
                s = Session(agent=AgentType.CLAUDE, prompt=f"prompt {i}")
                await store.create_session(s)
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "prompt" in result.stdout.lower()

    def test_list_filter_agent(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            await store.create_session(Session(agent=AgentType.CLAUDE, prompt="claude task"))
            await store.create_session(Session(agent=AgentType.CODEX, prompt="codex task"))
            await store.close()

        asyncio.run(_setup())
        result = runner.invoke(app, ["list", "--agent", "claude"])
        assert result.exit_code == 0
        assert "claude task" in result.stdout.lower()

    def test_list_shows_full_uuid_by_default(self, mock_db: Path):
        """v0.2.13: ``list`` must show the full 36-char UUID by default so
        users can copy-paste it directly into ``status`` / ``tail`` / ``kill``.
        Regression: pre-v0.2.13 truncated to ``id[:8]`` which made the rest of
        the UUID unfindable without first running ``status`` to discover it.

        The UUID may be folded across multiple lines in narrow terminals
        (rich table is terminal-width-aware), so we assert on the visible
        fragments rather than a single substring match.
        """
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="uuid check")
            await store.create_session(s)
            await store.close()
            return s.id

        full_id = asyncio.run(_setup())
        # Sanity: Session.id is a UUID4 (36 chars including dashes)
        assert len(full_id) == 36, f"expected 36-char UUID, got {len(full_id)}"

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        # The first 8 chars must appear — this is the same as the legacy
        # truncation, so a regression that re-introduced id[:8] would
        # still pass this assertion. To distinguish, we also check the
        # suffix (last 8 chars) appears somewhere in the output. A pure
        # id[:8] regression would never emit the suffix.
        assert full_id[:8] in result.stdout, (
            f"first 8 chars of UUID missing:\n{result.stdout}"
        )
        assert full_id[-8:] in result.stdout, (
            f"last 8 chars of UUID missing — looks like id[:8] regression:\n{result.stdout}"
        )
        # And the full id must be present once we strip rich's table
        # whitespace (which folds long cells across lines).
        import re as _re
        stripped = _re.sub(r"\s+", "", result.stdout)
        assert full_id in stripped, (
            f"full UUID {full_id!r} not in list output (after stripping whitespace):\n{result.stdout}"
        )

    def test_list_short_id_flag_truncates(self, mock_db: Path):
        """v0.2.13: ``--short-id`` opts back into the compact 8-char view
        for users who prefer it (the table fits more rows on screen).
        """
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="short id check")
            await store.create_session(s)
            await store.close()
            return s.id

        full_id = asyncio.run(_setup())

        result = runner.invoke(app, ["list", "--short-id"])
        assert result.exit_code == 0
        # The short form (first 8 chars) appears, but the full UUID does NOT
        # — otherwise the flag is a no-op.
        assert full_id[:8] in result.stdout
        assert full_id not in result.stdout, (
            f"full UUID unexpectedly present in --short-id output:\n{result.stdout}"
        )

    def test_list_short_id_default_off(self, mock_db: Path):
        """v0.2.13: ``--short-id`` must default to OFF. Verifies the
        boolean is wired as a flag, not a positional argument.
        """
        import asyncio
        import re as _re

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="default off check")
            await store.create_session(s)
            await store.close()
            return s.id

        full_id = asyncio.run(_setup())

        # Just `list` (no flag) must show the full UUID.
        result_default = runner.invoke(app, ["list"])
        stripped = _re.sub(r"\s+", "", result_default.stdout)
        assert full_id in stripped, (
            f"default `list` does not show full UUID:\n{result_default.stdout}"
        )


class TestSearchCommand:
    def test_search_no_results(self, mock_db: Path):
        result = runner.invoke(app, ["search", "nonexistent"])
        assert result.exit_code == 0
        assert "no matching" in result.stdout.lower()

    def test_search_shows_full_uuid(self, mock_db: Path):
        """v0.2.13: ``search`` output must show the full 36-char UUID so
        the user can pipe it into ``tail`` / ``kill`` directly. Previously
        truncated to ``session_id[:8]`` which made tail/kill unusable.
        """
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="marker-prompt")
            await store.create_session(s)
            from coding_agents.models import Event, EventType
            await store.append_events([Event(
                session_id=s.id,
                channel="stdout",
                seq=1,
                type=EventType.STDOUT,
                data="marker-output line",
            )])
            await store.close()
            return s.id

        full_id = asyncio.run(_setup())
        result = runner.invoke(app, ["search", "marker-output"])
        assert result.exit_code == 0
        # The last 8 chars of the UUID must appear — this is a strong
        # signal that the pre-v0.2.13 `session_id[:8]` truncation has
        # been removed. We don't try substring-matching the full UUID
        # because rich's formatting can split the output across lines.
        assert full_id[-8:] in result.stdout, (
            f"last 8 chars of UUID missing in search output — looks like "
            f"session_id[:8] regression:\n{result.stdout}"
        )
        # And the first 8 must also be present (it was, but if not, that's
        # also a regression).
        assert full_id[:8] in result.stdout


class TestTagCommand:
    def test_add_tag(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["tag", sid, "important"])
        assert result.exit_code == 0
        assert "added" in result.stdout.lower()

    def test_remove_tag(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.add_tag(s.id, "important")
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["tag", "-r", sid, "important"])
        assert result.exit_code == 0
        assert "removed" in result.stdout.lower()


class TestKillCommand:
    def test_kill_running(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.update_session(s.id, status=SessionStatus.RUNNING)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["kill", sid])
        assert result.exit_code == 0
        assert "killed" in result.stdout.lower()

    def test_kill_already_completed(self, mock_db: Path):
        import asyncio

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            s = Session(agent=AgentType.CLAUDE, prompt="test")
            await store.create_session(s)
            await store.update_session(s.id, status=SessionStatus.COMPLETED)
            await store.close()
            return s.id

        sid = asyncio.run(_setup())
        result = runner.invoke(app, ["kill", sid])
        assert result.exit_code == 0
        assert "already" in result.stdout.lower()


class TestRecoverCommand:
    def test_recover(self, mock_db: Path):
        result = runner.invoke(app, ["recover"])
        assert result.exit_code == 0
        assert "marked" in result.stdout.lower()


class TestDispatchOutputContract:
    """Verify dispatch output is bounded and safe for OpenClaw exec (1MB buffer).

    Contract:
    - Only session_id + one JSON result line on stdout/stderr.
    - Intermediate stdout/stderr must NOT appear in CLI output.
    - All events are persisted to SQLite and reachable via `tail` / `status`.
    """

    def test_dispatch_output_is_bounded(self, mock_db: Path, tmp_path: Path):
        """`dispatch` must NOT echo the agent's stdout line-by-line.

        Old behavior (pre-v0.2.6) wrote one annotated line per event,
        which easily exceeded the OpenClaw exec 1MB buffer.
        """
        import sys
        from unittest.mock import patch

        # Simulate a chatty agent that emits many lines
        fake_command = [
            sys.executable,
            "-c",
            "for i in range(100): print(f'chatty line {i}')",
        ]

        class FakeAdapter:
            def build_command(self, prompt, config):
                return fake_command

        with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
            result = runner.invoke(
                app,
                ["dispatch", "claude", "test prompt"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Critical: the chatty agent's output must NOT be in CLI output
        assert "chatty line 0" not in result.output
        assert "chatty line 99" not in result.output
        # session_id line and JSON result must be present
        assert "session_id=" in result.output
        # Result line is a JSON object with at least session_id + exit_code
        import json as _json
        for line in result.output.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                parsed = _json.loads(line)
                if "session_id" in parsed and "exit_code" in parsed:
                    break
        else:
            pytest.fail(f"no result JSON line found in: {result.output!r}")

    def test_dispatch_does_not_accept_stream_flag(self):
        """The --stream flag was removed in v0.2.6. dispatch must reject it."""
        result = runner.invoke(
            app,
            ["dispatch", "claude", "test", "--stream"],
        )
        assert result.exit_code != 0
        # Should fail with "no such option"
        assert "no such option" in result.output or "--stream" in result.output


class TestVersionCommand:
    """Test --version flag works correctly."""

    def test_version_flag(self):
        """`coding-agents --version` should show version and exit 0."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "coding-agents" in result.stdout
        # Should contain a version string like "0.2.31" or "unknown" (if not installed)
        assert any(c.isdigit() for c in result.stdout) or "unknown" in result.stdout

    def test_version_short_flag(self):
        """`coding-agents -v` should also work."""
        result = runner.invoke(app, ["-v"])
        assert result.exit_code == 0
        assert "coding-agents" in result.stdout


class TestAutoCleanupPoll:
    """Test auto-cleanup in poll command."""

    def test_auto_clean_enabled_by_default(self, mock_db: Path):
        """poll should auto-clean stuck sessions by default."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            # Create a pending session 3 minutes ago (should be auto-failed)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # poll with default --auto-clean should fail the pending session
        result = runner.invoke(app, ["poll", "--format", "json"])
        assert result.exit_code == 0
        assert "Auto-cleaned" in result.stdout or "auto-cleaned" in result.stdout.lower()

        # Verify the session was marked as failed
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.FAILED

    def test_no_auto_clean_flag(self, mock_db: Path):
        """poll with --no-auto-clean should not clean stuck sessions."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # poll with --no-auto-clean should NOT fail the pending session
        result = runner.invoke(app, ["poll", "--format", "json", "--no-auto-clean"])
        assert result.exit_code == 0

        # Verify session is still pending
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.PENDING


class TestAutoCleanupStatus:
    """Test auto-cleanup in status command."""

    def test_status_auto_clean_stuck_pending(self, mock_db: Path):
        """status should auto-clean stuck pending sessions."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # status should auto-clean the stuck session
        result = runner.invoke(app, ["status", sid])
        assert result.exit_code == 0
        assert "cleaned up" in result.stdout.lower()

        # Verify session was marked as failed
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.FAILED

    def test_status_no_auto_clean(self, mock_db: Path):
        """status with --no-auto-clean should not clean stuck sessions."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # status with --no-auto-clean should NOT fail the session
        result = runner.invoke(app, ["status", sid, "--no-auto-clean"])
        assert result.exit_code == 0

        # Verify session is still pending
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.PENDING

    def test_status_quiet_flag(self, mock_db: Path):
        """status with --quiet should suppress cleanup message."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.PENDING,
                created_at=now - timedelta(minutes=3),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # status with --quiet should not show cleanup message
        result = runner.invoke(app, ["status", sid, "--quiet"])
        assert result.exit_code == 0
        assert "cleaned up" not in result.stdout.lower()

    def test_status_auto_clean_orphaned_running(self, mock_db: Path):
        """status should auto-clean running sessions with no heartbeat > 24h."""
        import asyncio
        from datetime import datetime, timedelta, timezone

        async def _setup():
            store = SQLiteStorage(mock_db)
            await store.initialize()
            now = datetime.now(timezone.utc)
            session = Session(
                agent=AgentType.CLAUDE,
                prompt="test",
                workdir="/tmp",
                status=SessionStatus.RUNNING,
                started_at=now - timedelta(hours=25),
                last_heartbeat_at=now - timedelta(hours=25),
            )
            await store.create_session(session)
            await store.close()
            return session.id

        sid = asyncio.run(_setup())

        # status should auto-clean the orphaned session
        result = runner.invoke(app, ["status", sid])
        assert result.exit_code == 0
        assert "cleaned up" in result.stdout.lower()

        # Verify session was marked as orphaned
        async def check():
            s = SQLiteStorage(mock_db)
            await s.initialize()
            sess = await s.get_session(sid)
            await s.close()
            return sess.status if sess else None

        status = asyncio.run(check())
        assert status == SessionStatus.ORPHANED
