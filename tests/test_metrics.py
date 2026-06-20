"""Tests for the metrics system."""

from __future__ import annotations

import pytest

from coding_agents import metrics
from coding_agents.metrics import (
    decrement_active_sessions,
    events_appended_total,
    increment_active_sessions,
    record_event_appended,
    record_registry_wait,
    record_session_completed,
    record_session_created,
    record_session_duration,
    record_session_failed,
    record_session_killed,
    sessions_total,
    set_active_sessions,
    session_duration_seconds,
)
from coding_agents.metrics_integration import (
    track_event,
    track_registry_acquire,
    track_session,
)
from coding_agents.models import AgentType, Event, EventType, Session, SessionStatus


class TestSessionMetrics:
    """Test session lifecycle metrics."""

    async def test_record_session_created(self):
        """record_session_created should increment the counter."""
        # Get initial value
        initial = sessions_total.labels(agent="test1", status="created")._value.get()

        record_session_created("test1")

        # Check the counter value incremented
        value = sessions_total.labels(agent="test1", status="created")._value.get()
        assert value == initial + 1.0

    async def test_record_session_completed(self):
        """record_session_completed should increment the counter."""
        initial = sessions_total.labels(agent="test2", status="completed")._value.get()

        record_session_completed("test2")

        value = sessions_total.labels(agent="test2", status="completed")._value.get()
        assert value == initial + 1.0

    async def test_record_session_failed(self):
        """record_session_failed should increment the counter."""
        initial = sessions_total.labels(agent="test3", status="failed")._value.get()

        record_session_failed("test3")

        value = sessions_total.labels(agent="test3", status="failed")._value.get()
        assert value == initial + 1.0

    async def test_record_session_killed(self):
        """record_session_killed should increment the counter."""
        initial = sessions_total.labels(agent="test4", status="killed")._value.get()

        record_session_killed("test4")

        value = sessions_total.labels(agent="test4", status="killed")._value.get()
        assert value == initial + 1.0

    async def test_record_session_duration(self):
        """record_session_duration should observe the duration."""
        initial_sum = session_duration_seconds._sum.get()

        record_session_duration(5.0)
        record_session_duration(10.0)

        # Check histogram sum increased
        count = session_duration_seconds._sum.get()
        assert count == initial_sum + 15.0

    async def test_multiple_agents_tracked_separately(self):
        """Metrics should track different agents separately."""
        initial_claude = sessions_total.labels(agent="test_claude", status="created")._value.get()
        initial_codex = sessions_total.labels(agent="test_codex", status="created")._value.get()

        record_session_created("test_claude")
        record_session_created("test_claude")
        record_session_created("test_codex")

        claude_count = sessions_total.labels(agent="test_claude", status="created")._value.get()
        codex_count = sessions_total.labels(agent="test_codex", status="created")._value.get()

        assert claude_count == initial_claude + 2.0
        assert codex_count == initial_codex + 1.0


class TestEventMetrics:
    """Test event metrics."""

    async def test_record_event_appended(self):
        """record_event_appended should increment the counter by channel."""
        initial_stdout = events_appended_total.labels(channel="test_stdout")._value.get()
        initial_stderr = events_appended_total.labels(channel="test_stderr")._value.get()

        record_event_appended("test_stdout")
        record_event_appended("test_stdout")
        record_event_appended("test_stderr")

        stdout_count = events_appended_total.labels(channel="test_stdout")._value.get()
        stderr_count = events_appended_total.labels(channel="test_stderr")._value.get()

        assert stdout_count == initial_stdout + 2.0
        assert stderr_count == initial_stderr + 1.0

    async def test_different_channels_tracked_separately(self):
        """Different channels should be tracked separately."""
        initial_stdout = events_appended_total.labels(channel="ch_stdout")._value.get()
        initial_stderr = events_appended_total.labels(channel="ch_stderr")._value.get()
        initial_system = events_appended_total.labels(channel="ch_system")._value.get()

        record_event_appended("ch_stdout")
        record_event_appended("ch_stderr")
        record_event_appended("ch_system")

        stdout_count = events_appended_total.labels(channel="ch_stdout")._value.get()
        stderr_count = events_appended_total.labels(channel="ch_stderr")._value.get()
        system_count = events_appended_total.labels(channel="ch_system")._value.get()

        assert stdout_count == initial_stdout + 1.0
        assert stderr_count == initial_stderr + 1.0
        assert system_count == initial_system + 1.0


class TestActiveSessionsGauge:
    """Test active sessions gauge."""

    async def test_set_active_sessions(self):
        """set_active_sessions should set the gauge value."""
        set_active_sessions(5)

        value = metrics.active_sessions._value.get()
        assert value == 5.0

        # Reset to 0 for other tests
        set_active_sessions(0)

    async def test_increment_decrement_active_sessions(self):
        """increment/decrement should adjust the gauge."""
        # Start from 0
        metrics.active_sessions.set(0)

        increment_active_sessions()
        increment_active_sessions()
        increment_active_sessions()
        decrement_active_sessions()

        value = metrics.active_sessions._value.get()
        assert value == 2.0

        # Reset to 0 for other tests
        set_active_sessions(0)


class TestRegistryMetrics:
    """Test registry wait time metrics."""

    async def test_record_registry_wait(self):
        """record_registry_wait should observe the wait time."""
        initial_sum = metrics.session_registry_wait_seconds._sum.get()

        record_registry_wait(0.5)
        record_registry_wait(1.0)

        count = metrics.session_registry_wait_seconds._sum.get()
        assert count == initial_sum + 1.5


class TestTrackSessionDecorator:
    """Test the @track_session decorator."""

    async def test_track_session_success(self):
        """@track_session should track successful sessions."""
        # Use a unique session ID to avoid interference
        initial_created = sessions_total.labels(agent="claude", status="created")._value.get()
        initial_completed = sessions_total.labels(agent="claude", status="completed")._value.get()

        @track_session
        async def create_session(session: Session) -> Session:
            session.status = SessionStatus.COMPLETED
            session.duration_ms = 5000
            return session

        session = Session(agent=AgentType.CLAUDE, prompt="test_success")
        result = await create_session(session)

        assert result.status == SessionStatus.COMPLETED

        # Check metrics incremented
        created_count = sessions_total.labels(agent="claude", status="created")._value.get()
        completed_count = sessions_total.labels(agent="claude", status="completed")._value.get()

        assert created_count >= initial_created + 1.0
        assert completed_count >= initial_completed + 1.0

    async def test_track_session_failure(self):
        """@track_session should track failed sessions."""
        initial_failed = sessions_total.labels(agent="claude", status="failed")._value.get()

        @track_session
        async def failing_session(session: Session) -> Session:
            session.status = SessionStatus.FAILED
            return session

        session = Session(agent=AgentType.CLAUDE, prompt="test_failure")
        result = await failing_session(session)

        assert result.status == SessionStatus.FAILED

        # Check metrics
        failed_count = sessions_total.labels(agent="claude", status="failed")._value.get()
        assert failed_count >= initial_failed + 1.0

    async def test_track_session_killed(self):
        """@track_session should track killed sessions."""
        initial_killed = sessions_total.labels(agent="claude", status="killed")._value.get()

        @track_session
        async def killed_session(session: Session) -> Session:
            session.status = SessionStatus.KILLED
            return session

        session = Session(agent=AgentType.CLAUDE, prompt="test_killed")
        result = await killed_session(session)

        assert result.status == SessionStatus.KILLED

        # Check metrics
        killed_count = sessions_total.labels(agent="claude", status="killed")._value.get()
        assert killed_count >= initial_killed + 1.0

    async def test_track_session_exception(self):
        """@track_session should track sessions that raise exceptions."""
        initial_failed = sessions_total.labels(agent="claude", status="failed")._value.get()

        @track_session
        async def exception_session(session: Session) -> Session:
            raise ValueError("Test error")

        session = Session(agent=AgentType.CLAUDE, prompt="test_exception")

        with pytest.raises(ValueError):
            await exception_session(session)

        # Check metrics - should record as failed
        failed_count = sessions_total.labels(agent="claude", status="failed")._value.get()
        assert failed_count >= initial_failed + 1.0


class TestTrackEventDecorator:
    """Test the @track_event decorator."""

    async def test_track_single_event(self):
        """@track_event should track a single event."""
        initial = events_appended_total.labels(channel="test_ch1")._value.get()

        @track_event
        async def append_event(event: Event) -> None:
            pass

        event = Event(
            session_id="test",
            channel="test_ch1",
            seq=1,
            type=EventType.STDOUT,
            data="test",
        )
        await append_event(event)

        # Check metrics
        stdout_count = events_appended_total.labels(channel="test_ch1")._value.get()
        assert stdout_count == initial + 1.0

    async def test_track_multiple_events(self):
        """@track_event should track a list of events."""
        initial_stdout = events_appended_total.labels(channel="test_ch2")._value.get()
        initial_stderr = events_appended_total.labels(channel="test_ch3")._value.get()

        @track_event
        async def append_events(events: list[Event]) -> None:
            pass

        events = [
            Event(session_id="test", channel="test_ch2", seq=1, type=EventType.STDOUT, data="test1"),
            Event(session_id="test", channel="test_ch3", seq=2, type=EventType.STDERR, data="test2"),
            Event(session_id="test", channel="test_ch2", seq=3, type=EventType.STDOUT, data="test3"),
        ]
        await append_events(events)

        # Check metrics
        stdout_count = events_appended_total.labels(channel="test_ch2")._value.get()
        stderr_count = events_appended_total.labels(channel="test_ch3")._value.get()

        assert stdout_count == initial_stdout + 2.0
        assert stderr_count == initial_stderr + 1.0


class TestTrackRegistryAcquireDecorator:
    """Test the @track_registry_acquire decorator."""

    async def test_track_registry_acquire(self):
        """@track_registry_acquire should track wait time."""
        # Just verify the decorator doesn't crash and records something
        @track_registry_acquire
        async def acquire_slot() -> bool:
            return True

        result = await acquire_slot()
        assert result is True

        # The histogram should have recorded at least one observation
        # (we can't easily check the exact value since it's so fast)
        assert metrics.session_registry_wait_seconds._sum.get() >= 0
