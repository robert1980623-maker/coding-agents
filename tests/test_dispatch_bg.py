"""Tests for v0.2.17 fire-and-forget dispatch-bg command.

These tests verify that:
1. dispatch-bg command is registered
2. dispatch-bg returns session_id within ~1 second (not blocking)
3. Runner subprocess is actually spawned
4. Status query still works after wrapper exits
"""

import os
import subprocess
import time
import sqlite3
import pytest
import tempfile

CODING_AGENTS = "/Users/rowang/.local/bin/coding-agents"


def _test_db_path():
    """Use a temporary database to avoid polluting the real one."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return tmp.name


class TestDispatchBgCommandExists:
    """The dispatch-bg command must be registered."""

    def test_help_shows_command(self):
        result = subprocess.run(
            [CODING_AGENTS, "dispatch-bg", "--help"],
            capture_output=True, text=True, timeout=5
        )
        assert "fire-and-forget" in result.stdout.lower() or "running" in result.stdout.lower(), (
            "dispatch-bg --help should describe the fire-and-forget behavior"
        )
        assert result.returncode == 0

    def test_command_lists_in_help(self):
        result = subprocess.run(
            [CODING_AGENTS, "--help"], capture_output=True, text=True, timeout=5
        )
        assert "dispatch-bg" in result.stdout, (
            "dispatch-bg should appear in the main --help output"
        )


class TestDispatchBgReturnsQuickly:
    """dispatch-bg must return within ~5 seconds (way under 30s OpenClaw timeout)."""

    def test_simple_prompt_returns_under_5s(self):
        start = time.time()
        result = subprocess.run(
            [CODING_AGENTS, "dispatch-bg", "claude", "echo hello only",
             "--workdir", "/tmp"],
            capture_output=True, text=True, timeout=10
        )
        elapsed = time.time() - start
        assert result.returncode == 0, f"dispatch-bg failed: {result.stderr}"
        assert "session_id=" in result.stdout, "Must print session_id"
        assert elapsed < 5, f"dispatch-bg took {elapsed:.1f}s, must be < 5s"

    def test_output_is_bounded(self):
        result = subprocess.run(
            [CODING_AGENTS, "dispatch-bg", "claude", "echo hello only",
             "--workdir", "/tmp"],
            capture_output=True, text=True, timeout=10
        )
        # Output should be just 2 lines: session_id + JSON
        lines = [l for l in result.stdout.strip().split("\n") if l]
        assert len(lines) <= 3, f"Output should be ≤3 lines, got {len(lines)}"


class TestDispatchBgSpawnsRunner:
    """The runner subprocess must actually be spawned."""

    def test_session_recorded_in_db(self):
        result = subprocess.run(
            [CODING_AGENTS, "dispatch-bg", "claude", "echo hello only",
             "--workdir", "/tmp"],
            capture_output=True, text=True, timeout=10
        )
        # Extract session_id from output
        session_id = None
        for line in result.stdout.split("\n"):
            if line.startswith("session_id="):
                session_id = line.split("=", 1)[1].strip()
                break
        assert session_id, f"No session_id in: {result.stdout}"

        # Check that the session exists in the DB
        db_path = os.path.expanduser("~/.coding-agents/data.db")
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, agent FROM sessions WHERE id = ?",
                (session_id,)
            ).fetchone()
        assert row is not None, f"Session {session_id} not in DB"
        assert row[0] in ("running", "pending", "completed", "failed"), (
            f"Unexpected status: {row[0]}"
        )


@pytest.mark.skip(reason="Integration test; can hang if Claude CLI is slow")
class TestDispatchBgCompletesIndependently:
    """The runner subprocess must complete even if the wrapper is gone."""

    def test_runner_eventually_completes(self):
        result = subprocess.run(
            [CODING_AGENTS, "dispatch-bg", "claude", "echo hello only",
             "--workdir", "/tmp"],
            capture_output=True, text=True, timeout=10
        )
        session_id = None
        for line in result.stdout.split("\n"):
            if line.startswith("session_id="):
                session_id = line.split("=", 1)[1].strip()
                break

        # Poll for completion
        db_path = os.path.expanduser("~/.coding-agents/data.db")
        for _ in range(60):  # 60 seconds max
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT status FROM sessions WHERE id = ?",
                    (session_id,)
                ).fetchone()
            if row and row[0] in ("completed", "failed"):
                assert row[0] == "completed", f"Expected completed, got {row[0]}"
                return
            time.sleep(1)
        pytest.fail(f"Session {session_id} did not complete within 60s")
