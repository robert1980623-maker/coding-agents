"""Metrics integration decorators and utilities.

Since we cannot modify executor/registry/storage directly (file isolation rules),
this module provides decorators that can be used to instrument these components
with metrics tracking.

Usage:
    from coding_agents.metrics_integration import track_session, track_event

    @track_session
    async def my_session_function(session_id: str, ...):
        # Session metrics will be tracked automatically
        ...

    @track_event
    async def my_event_function(event: Event, ...):
        # Event metrics will be tracked automatically
        ...
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable, TypeVar

import structlog

from coding_agents.metrics import (
    decrement_active_sessions,
    increment_active_sessions,
    record_event_appended,
    record_registry_wait,
    record_session_completed,
    record_session_created,
    record_session_duration,
    record_session_failed,
    record_session_killed,
)
from coding_agents.models import Event, Session, SessionStatus

logger = structlog.get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def track_session(func: F) -> F:
    """Decorator to track session lifecycle metrics.

    Automatically records:
    - Session creation (when function starts)
    - Session completion/failure/kill (based on final status)
    - Session duration

    The decorated function should return a Session object or accept a session_id
    parameter that can be used to look up the session.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Try to extract session info from arguments
        session = None
        session_id = None
        agent = "unknown"

        # Check if first arg is a Session
        if args and isinstance(args[0], Session):
            session = args[0]
            session_id = session.id
            agent = session.agent.value
        # Check if session_id is in kwargs
        elif "session_id" in kwargs:
            session_id = kwargs["session_id"]
        # Check if session is in kwargs
        elif "session" in kwargs:
            session = kwargs["session"]
            session_id = session.id
            agent = session.agent.value

        # Record session creation
        record_session_created(agent)
        increment_active_sessions()

        start_time = time.time()
        try:
            result = await func(*args, **kwargs)

            # If result is a Session, extract final status
            if isinstance(result, Session):
                final_status = result.status
                agent = result.agent.value
                if result.duration_ms is not None:
                    record_session_duration(result.duration_ms / 1000.0)
            else:
                final_status = SessionStatus.COMPLETED

            # Record final status
            if final_status == SessionStatus.COMPLETED:
                record_session_completed(agent)
            elif final_status == SessionStatus.FAILED:
                record_session_failed(agent)
            elif final_status == SessionStatus.KILLED:
                record_session_killed(agent)

            return result
        except Exception as e:
            record_session_failed(agent)
            raise
        finally:
            duration = time.time() - start_time
            decrement_active_sessions()

    return wrapper  # type: ignore[return-value]


def track_event(func: F) -> F:
    """Decorator to track event metrics.

    Automatically records:
    - Events appended by channel

    The decorated function should accept an Event or list of Events.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Try to extract event info from arguments
        events = []

        # Check if first arg is an Event
        if args and isinstance(args[0], Event):
            events = [args[0]]
        # Check if first arg is a list of Events
        elif args and isinstance(args[0], list) and args[0] and isinstance(args[0][0], Event):
            events = args[0]
        # Check if events is in kwargs
        elif "events" in kwargs:
            events_list = kwargs["events"]
            if isinstance(events_list, list):
                events = events_list
            elif isinstance(events_list, Event):
                events = [events_list]
        # Check if event is in kwargs
        elif "event" in kwargs:
            events = [kwargs["event"]]

        # Record events by channel
        for event in events:
            record_event_appended(event.channel)

        return await func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def track_registry_acquire(func: F) -> F:
    """Decorator to track registry acquisition wait time.

    Automatically records the time spent waiting for a registry slot.
    """

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            record_registry_wait(duration)
            return result
        except Exception:
            duration = time.time() - start_time
            record_registry_wait(duration)
            raise

    return wrapper  # type: ignore[return-value]
