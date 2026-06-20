"""Tests for data models."""

from __future__ import annotations

from datetime import datetime, timezone

from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
    TERMINAL_STATUSES,
    WatchPattern,
)


class TestAgentType:
    def test_values(self):
        assert AgentType.CLAUDE.value == "claude"
        assert AgentType.CODEX.value == "codex"

    def test_from_string(self):
        assert AgentType("claude") == AgentType.CLAUDE
        assert AgentType("codex") == AgentType.CODEX


class TestSessionStatus:
    def test_all_statuses(self):
        names = {s.name for s in SessionStatus}
        assert names == {
            "PENDING",
            "RUNNING",
            "COMPLETED",
            "FAILED",
            "KILLED",
            "TIMEOUT",
            "ORPHANED",
        }

    def test_is_terminal(self):
        assert not SessionStatus.PENDING.is_terminal
        assert not SessionStatus.RUNNING.is_terminal
        assert SessionStatus.COMPLETED.is_terminal
        assert SessionStatus.FAILED.is_terminal
        assert SessionStatus.KILLED.is_terminal
        assert SessionStatus.TIMEOUT.is_terminal
        assert SessionStatus.ORPHANED.is_terminal


class TestTerminalStatuses:
    def test_contains_terminal(self):
        for s in [
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.KILLED,
            SessionStatus.TIMEOUT,
            SessionStatus.ORPHANED,
        ]:
            assert s in TERMINAL_STATUSES

    def test_excludes_non_terminal(self):
        for s in [SessionStatus.PENDING, SessionStatus.RUNNING]:
            assert s not in TERMINAL_STATUSES


class TestEventType:
    def test_values(self):
        assert EventType.SESSION_START.value == "session.start"
        assert EventType.STDOUT.value == "stdout"
        assert EventType.STDERR.value == "stderr"
        assert EventType.RESULT.value == "result"
        assert EventType.ERROR.value == "error"


class TestSession:
    def test_defaults(self):
        s = Session()
        assert s.status == SessionStatus.PENDING
        assert s.pid is None
        assert s.exit_code is None
        assert s.metadata == {}

    def test_custom_values(self):
        s = Session(
            agent=AgentType.CODEX,
            prompt="test prompt",
            workdir="/tmp",
            cost_usd=1.23,
            input_tokens=100,
            output_tokens=50,
        )
        assert s.agent == AgentType.CODEX
        assert s.prompt == "test prompt"
        assert s.cost_usd == 1.23

    def test_auto_id(self):
        s1 = Session()
        s2 = Session()
        assert s1.id != s2.id
        # UUID format
        assert len(s1.id) == 36

    def test_created_at_default(self):
        s = Session()
        assert isinstance(s.created_at, datetime)
        assert s.created_at.tzinfo is not None


class TestEvent:
    def test_defaults(self):
        e = Event()
        assert e.channel == "stdout"
        assert e.seq == 0
        assert e.type == EventType.STDOUT
        assert e.data == ""
        assert e.raw_json is None
        assert e.metadata == {}

    def test_custom(self):
        e = Event(
            session_id="abc",
            channel="stderr",
            seq=42,
            type=EventType.STDERR,
            data="error message",
        )
        assert e.session_id == "abc"
        assert e.seq == 42
        assert e.type == EventType.STDERR


class TestExecutionConfig:
    def test_defaults(self):
        c = ExecutionConfig()
        assert c.timeout_seconds == 3600
        assert c.idle_timeout_seconds == 300
        assert c.max_memory_mb == 4096
        assert c.max_budget_usd == 10.0
        assert c.output_mode == "standard"
        assert c.line_limit == 8 * 1024 * 1024
        assert c.model is None
        assert c.env == {}
        assert c.watch_patterns == []

    def test_custom(self):
        c = ExecutionConfig(
            timeout_seconds=60,
            model="claude-sonnet-4-20250514",
            env={"KEY": "value"},
        )
        assert c.timeout_seconds == 60
        assert c.model == "claude-sonnet-4-20250514"
        assert c.env == {"KEY": "value"}


class TestWatchPattern:
    def test_defaults(self):
        wp = WatchPattern(pattern="ERROR")
        assert wp.action == "notify"
        assert wp.callback is None

    def test_custom(self):
        wp = WatchPattern(pattern="FATAL", action="stop", callback="webhook")
        assert wp.pattern == "FATAL"
        assert wp.action == "stop"
        assert wp.callback == "webhook"
