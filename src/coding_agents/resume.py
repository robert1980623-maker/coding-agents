"""Session resume — restart a session from its last known sequence number.

Design notes (v0.2.0-S3 / P2-5):
- A session is resumable when its status is terminal AND exit code is 0
  (completed normally but interrupted) OR the status is ``KILLED`` /
  ``TIMEOUT`` (external interruption).
- ``ResumeInfo`` captures the minimum state needed to issue a ``--resume``
  command: the session ID, the last event sequence number, and the agent type.
- ``resume_session`` creates a **new** session (linked via metadata) and
  re-runs the agent with ``--resume`` so the agent CLI can continue from
  its last checkpoint.

Agent CLI support for ``--resume`` is agent-specific.  Use
:func:`enable_resume_support` from ``orchestrator.cli_integration`` to
monkey-patch an agent adapter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from coding_agents.agents.factory import get_agent
from coding_agents.executor import StreamExecutor
from coding_agents.models import (
    AgentType,
    Event,
    EventType,
    ExecutionConfig,
    Session,
    SessionStatus,
)
from coding_agents.orchestrator.cli_integration import build_resume_command
from coding_agents.storage.base import StorageBackend

logger = structlog.get_logger(__name__)

# Statuses that allow resume.  FAILED with exit_code != 0 is NOT resumable
# because the agent's internal state is likely corrupt.
RESUMABLE_STATUSES: frozenset[SessionStatus] = frozenset(
    {
        SessionStatus.COMPLETED,
        SessionStatus.KILLED,
        SessionStatus.TIMEOUT,
    }
)


@dataclass
class ResumeInfo:
    """State needed to resume a session."""

    session_id: str
    last_seq: int
    last_event_data: Optional[str] = None
    agent_type: AgentType = AgentType.CLAUDE
    prompt: str = ""
    exit_code: Optional[int] = None


class ResumeNotSupportedError(Exception):
    """Raised when a session cannot be resumed."""


async def can_resume(session_id: str, storage: StorageBackend) -> bool:
    """Check whether *session_id* can be resumed.

    A session is resumable when:
    - It exists in storage.
    - Its status is in :data:`RESUMABLE_STATUSES`.
    - It has at least one event (so we know where to continue from).
    - If ``exit_code`` is set, it must be ``0`` (graceful completion or
      external interruption — not a crash).
    """
    session = await storage.get_session(session_id)
    if session is None:
        return False

    if session.status not in RESUMABLE_STATUSES:
        return False

    # Failed with non-zero exit code → agent state is unreliable
    if session.exit_code is not None and session.exit_code != 0:
        return False

    events = await storage.get_events(session_id)
    return len(events) > 0


def prepare_resume_command(
    session: Session,
    agent_type: AgentType,
    config: ExecutionConfig,
    last_seq: int,
) -> list[str]:
    """Build the command-line invocation for resuming a session.

    Uses :func:`build_resume_command` which knows about agent-specific
    ``--resume`` flags.
    """
    agent = get_agent(agent_type)
    base_command = agent.build_command(session.prompt, config)
    return build_resume_command(
        base_command,
        session_id=session.id,
        last_seq=last_seq,
        agent_type=agent_type,
    )


async def get_resume_info(
    session_id: str, storage: StorageBackend
) -> Optional[ResumeInfo]:
    """Collect :class:`ResumeInfo` for a session.

    Returns ``None`` if the session does not exist or has no events.
    """
    session = await storage.get_session(session_id)
    if session is None:
        return None

    events = await storage.get_events(session_id)
    if not events:
        return None

    last_event = events[-1]

    return ResumeInfo(
        session_id=session_id,
        last_seq=last_event.seq,
        last_event_data=last_event.data,
        agent_type=(
            session.agent
            if isinstance(session.agent, AgentType)
            else AgentType(session.agent)
        ),
        prompt=session.prompt,
        exit_code=session.exit_code,
    )


async def resume_session(
    session_id: str,
    storage: StorageBackend,
    agent_factory: Any | None = None,
    new_session_id: Optional[str] = None,
) -> tuple[str, list[Event]]:
    """Resume a session from its last known event.

    Creates a **new** session linked to the original via metadata, then
    executes the agent with ``--resume`` arguments.

    Parameters
    ----------
    session_id:
        ID of the session to resume.
    storage:
        Storage backend.
    agent_factory:
        Optional override for :func:`get_agent` (useful for testing).
    new_session_id:
        Optional explicit ID for the new session.

    Returns
    -------
    ``(new_session_id, events)`` — the events collected during the resumed
    execution.

    Raises
    ------
    ResumeNotSupportedError
        If the session cannot be resumed.
    """
    if not await can_resume(session_id, storage):
        raise ResumeNotSupportedError(
            f"Session {session_id} cannot be resumed"
        )

    resume_info = await get_resume_info(session_id, storage)
    if resume_info is None:
        raise ResumeNotSupportedError(
            f"No resume info for session {session_id}"
        )

    agent_type = resume_info.agent_type
    factory = agent_factory or get_agent
    agent = factory(agent_type)

    # Build base command and append resume flags
    config = ExecutionConfig()
    base_command = agent.build_command(resume_info.prompt, config)
    resume_command = build_resume_command(
        base_command,
        session_id=session_id,
        last_seq=resume_info.last_seq,
        agent_type=agent_type,
    )

    # Create new session
    sid = new_session_id or str(uuid.uuid4())
    new_session = Session(
        id=sid,
        agent=agent_type,
        prompt=resume_info.prompt,
        metadata={
            "resumed_from": session_id,
            "resume_from_seq": resume_info.last_seq,
        },
    )
    await storage.create_session(new_session)

    logger.info(
        "resuming_session",
        original_session=session_id,
        new_session=sid,
        last_seq=resume_info.last_seq,
    )

    # Execute
    executor = StreamExecutor(store=storage, config=config)
    events: list[Event] = []
    async for event in executor.execute(sid, resume_command, "."):
        events.append(event)

    return sid, events
