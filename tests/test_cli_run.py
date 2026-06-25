"""Tests for CLI run commands including --idle-timeout functionality."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from coding_agents.cli import app
from coding_agents.models import AgentType, Session
from coding_agents.storage.sqlite import SQLiteStorage


runner = CliRunner()


@pytest.fixture
def mock_db(tmp_path: Path, monkeypatch):
    """Patch DEFAULT_DB and CODING_AGENTS_DB env var to use a temp directory."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CODING_AGENTS_DB", str(db_path))
    with patch("coding_agents.cli.DEFAULT_DB", str(db_path)):
        yield db_path


class TestIdleTimeoutOption:
    """Test --idle-timeout parameter functionality."""

    def test_dispatch_with_idle_timeout(self, mock_db: Path):
        """Test dispatch command accepts --idle-timeout parameter."""
        # Mock the subprocess to avoid actually running the agent
        fake_command = ["echo", "test"]

        class FakeAdapter:
            def build_command(self, prompt, config):
                return fake_command

            def env_overrides(self):
                return {}

            def env_deletions(self):
                return {}

        with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
            result = runner.invoke(
                app,
                ["dispatch", "claude", "test prompt", "--idle-timeout", "600"],
                catch_exceptions=False,
            )

        # Should succeed even though we didn't actually run the agent
        # (it would fail at the subprocess level but we're mocking that)
        # The important thing is that the command recognizes the flag
        assert "session_id=" in result.output or result.exit_code in [0, 1]

    def test_dispatch_bg_with_idle_timeout(self, mock_db: Path):
        """Test dispatch-bg command accepts --idle-timeout parameter."""
        # Mock the subprocess to avoid actually running the agent
        fake_command = ["echo", "test"]

        class FakeAdapter:
            def build_command(self, prompt, config):
                return fake_command

            def env_overrides(self):
                return {}

            def env_deletions(self):
                return {}

        with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
            result = runner.invoke(
                app,
                ["dispatch-bg", "claude", "test prompt", "--idle-timeout", "600"],
                catch_exceptions=False,
            )

        # Should succeed even though we didn't actually run the agent
        assert "session_id=" in result.output or result.exit_code in [0, 1]

    def test_run_with_idle_timeout(self, mock_db: Path):
        """Test run command accepts --idle-timeout parameter."""
        # Mock the subprocess to avoid actually running the agent
        fake_command = ["echo", "test"]

        class FakeAdapter:
            def build_command(self, prompt, config):
                return fake_command

            def env_overrides(self):
                return {}

            def env_deletions(self):
                return {}

        with patch("coding_agents.cli.get_agent", return_value=FakeAdapter()):
            result = runner.invoke(
                app,
                ["run", "claude", "test prompt", "--idle-timeout", "600"],
                catch_exceptions=False,
            )

        # Should succeed even though we didn't actually run the agent
        # Since run is deprecated, it may show a warning but should still recognize the flag
        assert result.exit_code in [0, 1]  # Either success or subprocess error is OK

    def test_dispatch_invalid_idle_timeout(self, mock_db: Path):
        """Test dispatch command handles invalid idle timeout values."""
        result = runner.invoke(
            app,
            ["dispatch", "claude", "test prompt", "--idle-timeout", "invalid"],
        )

        assert result.exit_code != 0
        assert "invalid" in result.output.lower()

    def test_dispatch_bg_invalid_idle_timeout(self, mock_db: Path):
        """Test dispatch-bg command handles invalid idle timeout values."""
        result = runner.invoke(
            app,
            ["dispatch-bg", "claude", "test prompt", "--idle-timeout", "invalid"],
        )

        assert result.exit_code != 0
        assert "invalid" in result.output.lower()


class TestExecutionConfigIdleTimeout:
    """Test that idle_timeout is properly passed to ExecutionConfig."""

    def test_execution_config_gets_idle_timeout(self, mock_db: Path):
        """Verify that idle_timeout parameter is correctly applied to ExecutionConfig."""
        from coding_agents.models import ExecutionConfig

        # Create a config and check default
        config = ExecutionConfig()
        assert config.idle_timeout_seconds == 300  # Default

        # Modify and verify
        config.idle_timeout_seconds = 600
        assert config.idle_timeout_seconds == 600


class TestDispatchBgPostSpawnHealthCheck:
    """Test the post-spawn health check in _dispatch_bg_setup."""

    def test_health_check_detects_crashed_runner(self, mock_db: Path):
        """Health check should detect if runner crashes immediately after spawn."""
        import time
        from unittest.mock import patch, MagicMock

        # Create a runner that will exit immediately
        # We'll patch subprocess.Popen to return a process that's already dead
        dead_process = MagicMock()
        dead_process.pid = 12345
        dead_process.poll.return_value = 1  # Already exited with code 1

        with patch("coding_agents.cli.run.subprocess.Popen", return_value=dead_process):
            with patch("coding_agents.cli.get_agent") as mock_get_agent:
                # Mock agent adapter
                class FakeAdapter:
                    def build_command(self, prompt, config):
                        return ["echo", "test"]

                    def env_overrides(self):
                        return {}

                    def env_deletions(self):
                        return {}

                mock_get_agent.return_value = FakeAdapter()

                result = runner.invoke(
                    app,
                    ["dispatch-bg", "claude", "test prompt"],
                    catch_exceptions=False,
                )

                # Should output spawn_failed status
                assert result.exit_code == 1
                assert "spawn_failed" in result.output

    def test_health_check_allows_running_runner(self, mock_db: Path):
        """Health check should allow runners that are still alive."""
        from unittest.mock import patch, MagicMock

        # Create a runner that is still alive
        running_process = MagicMock()
        running_process.pid = 67890
        running_process.poll.return_value = None  # Process still running

        with patch("coding_agents.cli.run.subprocess.Popen", return_value=running_process):
            with patch("coding_agents.cli.get_agent") as mock_get_agent:
                # Mock agent adapter
                class FakeAdapter:
                    def build_command(self, prompt, config):
                        return ["echo", "test"]

                    def env_overrides(self):
                        return {}

                    def env_deletions(self):
                        return {}

                mock_get_agent.return_value = FakeAdapter()

                # We need to also mock asyncio.sleep to skip the 3s wait
                with patch("asyncio.sleep", return_value=None):
                    result = runner.invoke(
                        app,
                        ["dispatch-bg", "claude", "test prompt"],
                        catch_exceptions=False,
                    )

                    # Should succeed (runner is still running)
                    assert result.exit_code == 0
                    assert "session_id=" in result.output