"""Prometheus metrics for the coding-agents runtime.

This module defines all metrics tracked by the system:
- sessions_total: Counter for total sessions created/completed/failed/killed
- session_duration_seconds: Histogram of session durations
- events_appended_total: Counter for events by channel
- active_sessions: Gauge for currently running sessions
- session_registry_wait_seconds: Histogram of time waiting for registry slot
- subprocess_memory_bytes: Gauge for subprocess memory usage
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Session lifecycle metrics
sessions_total = Counter(
    "sessions_total",
    "Total number of sessions",
    ["agent", "status"],
)

session_duration_seconds = Histogram(
    "session_duration_seconds",
    "Session duration in seconds",
    buckets=[
        1, 5, 10, 30, 60,  # 1s to 1m
        300, 600, 1800, 3600,  # 5m to 1h
        7200, 14400, 28800,  # 2h to 8h
    ],
)

# Event metrics
events_appended_total = Counter(
    "events_appended_total",
    "Total number of events appended",
    ["channel"],
)

# Active sessions gauge
active_sessions = Gauge(
    "active_sessions",
    "Number of currently active sessions",
)

# Registry wait time
session_registry_wait_seconds = Histogram(
    "session_registry_wait_seconds",
    "Time spent waiting for a registry slot",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)

# Subprocess memory usage
subprocess_memory_bytes = Gauge(
    "subprocess_memory_bytes",
    "Memory usage of subprocess in bytes",
    ["session_id"],
)


def record_session_created(agent: str) -> None:
    """Record that a session was created."""
    sessions_total.labels(agent=agent, status="created").inc()


def record_session_completed(agent: str) -> None:
    """Record that a session completed successfully."""
    sessions_total.labels(agent=agent, status="completed").inc()


def record_session_failed(agent: str) -> None:
    """Record that a session failed."""
    sessions_total.labels(agent=agent, status="failed").inc()


def record_session_killed(agent: str) -> None:
    """Record that a session was killed."""
    sessions_total.labels(agent=agent, status="killed").inc()


def record_session_duration(duration_seconds: float) -> None:
    """Record the duration of a session."""
    session_duration_seconds.observe(duration_seconds)


def record_event_appended(channel: str) -> None:
    """Record that an event was appended."""
    events_appended_total.labels(channel=channel).inc()


def set_active_sessions(count: int) -> None:
    """Set the number of active sessions."""
    active_sessions.set(count)


def increment_active_sessions() -> None:
    """Increment the active sessions gauge."""
    active_sessions.inc()


def decrement_active_sessions() -> None:
    """Decrement the active sessions gauge."""
    active_sessions.dec()


def record_registry_wait(duration_seconds: float) -> None:
    """Record time spent waiting for a registry slot."""
    session_registry_wait_seconds.observe(duration_seconds)


def set_subprocess_memory(session_id: str, memory_bytes: int) -> None:
    """Set the memory usage for a subprocess."""
    subprocess_memory_bytes.labels(session_id=session_id).set(memory_bytes)


def remove_subprocess_memory(session_id: str) -> None:
    """Remove the memory gauge for a completed subprocess."""
    subprocess_memory_bytes.remove(session_id)
