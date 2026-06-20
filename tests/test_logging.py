"""Tests for structlog-based logging configuration."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import patch

import structlog

from coding_agents.logging_config import get_logger, setup_logging


class TestSetupLogging:
    def test_json_output_format(self, capsys):
        """Verify logs are emitted as JSON with expected fields."""
        setup_logging(level="DEBUG", json_output=True)
        logger = structlog.get_logger("test.json")
        logger.info("test_event", session_id="abc-123", seq=42)

        captured = capsys.readouterr()
        # Logging goes to stderr via our handler
        line = [l for l in captured.err.strip().split("\n") if l][-1]
        parsed = json.loads(line)
        assert parsed["event"] == "test_event"
        assert parsed["level"] == "info"
        assert parsed["session_id"] == "abc-123"
        assert parsed["seq"] == 42
        assert "timestamp" in parsed
        assert "logger" in parsed

    def test_console_output_mode(self, capsys):
        """Verify console mode emits human-readable output."""
        setup_logging(level="INFO", json_output=False)
        logger = structlog.get_logger("test.console")
        logger.info("hello_console")

        captured = capsys.readouterr()
        assert "hello_console" in captured.err

    def test_level_filtering(self, capsys):
        """Verify level filtering works."""
        setup_logging(level="WARNING", json_output=True)
        logger = structlog.get_logger("test.level")
        logger.info("should_not_appear")
        logger.warning("should_appear")

        captured = capsys.readouterr()
        lines = [l for l in captured.err.strip().split("\n") if l]
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["event"] == "should_appear"
        assert parsed["level"] == "warning"


class TestGetLogger:
    def test_returns_bound_logger(self):
        """Verify get_logger returns a logger with bound context."""
        setup_logging(level="DEBUG", json_output=True)
        logger = get_logger("test.bound", session_id="xyz")
        assert logger is not None
        # Logger should support standard methods
        assert hasattr(logger, "info")
        assert hasattr(logger, "error")
        assert hasattr(logger, "warning")

    def test_initial_values_bound(self, capsys):
        """Verify initial values appear in log output."""
        setup_logging(level="DEBUG", json_output=True)
        logger = get_logger("test.initial", session_id="sess-1")
        logger.info("with_context")

        captured = capsys.readouterr()
        line = [l for l in captured.err.strip().split("\n") if l][-1]
        parsed = json.loads(line)
        assert parsed["session_id"] == "sess-1"
