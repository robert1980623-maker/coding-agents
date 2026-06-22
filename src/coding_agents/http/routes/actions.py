"""Action routes (kill, recover, resume)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from coding_agents.http.auth import verify_token
from coding_agents.models import SessionStatus
from coding_agents.resume import (
    ResumeNotSupportedError,
    can_resume,
    get_resume_info,
    resume_session,
)
from coding_agents.storage.sqlite import SQLiteStorage

router = APIRouter(tags=["actions"])

logger = structlog.get_logger(__name__)


class KillResponse(BaseModel):
    """Response for kill action."""

    success: bool
    session_id: str
    message: str


class RecoverResponse(BaseModel):
    """Response for recover action."""

    recovered_count: int
    message: str


class ResumeResponse(BaseModel):
    """Response for resume action."""

    success: bool
    original_session_id: str
    new_session_id: str
    last_seq: int
    status: str
    message: str


@router.post("/sessions/{session_id}/kill", response_model=KillResponse)
async def kill_session(
    session_id: str,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> KillResponse:
    """Kill a running session.

    Sets the session status to KILLED. The executor's heartbeat checker
    will detect this and terminate the process.
    """
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    if session.status not in {SessionStatus.RUNNING, SessionStatus.PENDING}:
        return KillResponse(
            success=False,
            session_id=session_id,
            message=f"Session is already {session.status.value}",
        )

    await storage.update_session(
        session_id,
        status=SessionStatus.KILLED,
        finished_at=datetime.now(timezone.utc),
    )

    return KillResponse(
        success=True,
        session_id=session_id,
        message=f"Killed session {session_id}",
    )


@router.post("/recover", response_model=RecoverResponse)
async def recover_sessions(
    timeout_seconds: int = 300,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> RecoverResponse:
    """Recover orphaned sessions.

    Marks sessions that have been running for longer than *timeout_seconds*
    without a heartbeat as ORPHANED.
    """
    count = await storage.recover_orphaned_sessions(timeout_seconds=timeout_seconds)

    return RecoverResponse(
        recovered_count=count,
        message=f"Recovered {count} orphaned sessions",
    )


@router.post("/sessions/{session_id}/resume", response_model=ResumeResponse)
async def resume_session_endpoint(
    session_id: str,
    new_session_id: Optional[str] = None,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> ResumeResponse:
    """Resume a session from its last known event.

    A session is resumable when its status is ``COMPLETED``, ``KILLED``,
    or ``TIMEOUT`` AND it has at least one event recorded AND its exit
    code is ``None`` or ``0`` (graceful completion or external
    interruption — not a crash).

    The core :func:`resume_session` creates a *new* session linked to the
    original via metadata, then runs the agent with ``--resume`` flags so
    the agent CLI can continue from its last checkpoint.

    Args:
        session_id: ID of the session to resume.
        new_session_id: Optional explicit ID for the new (resumed) session.
            If omitted, the core generates a UUID4.
        token: Bearer token (injected by ``verify_token``).
        storage: Storage backend (injected by dependency).

    Returns:
        :class:`ResumeResponse` with the new session's id, the last
        event sequence number the resume picks up from, and the new
        session's current status.

    Raises:
        HTTPException: 404 if the original session does not exist;
            409 if the session exists but is not resumable;
            500 for unexpected errors (handled by the global handler).
    """
    # 1. Validate the session exists.
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    # 2. Pre-check resumability so we can return a precise 409 with a
    #    human-readable reason (status + exit code). The core
    #    resume_session() also re-checks via can_resume(); we keep this
    #    pre-check for a friendlier error message and to avoid spinning
    #    up the executor on a session that obviously cannot be resumed.
    if not await can_resume(session_id, storage):
        reason_parts = [f"status={session.status.value}"]
        if session.exit_code is not None:
            reason_parts.append(f"exit_code={session.exit_code}")
        reason = ", ".join(reason_parts)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Session {session_id} cannot be resumed ({reason}). "
                f"Only sessions with status COMPLETED, KILLED, or TIMEOUT "
                f"(with exit_code 0 or None, and at least one event) are "
                f"resumable."
            ),
        )

    # 3. Resolve the original session's last_seq so we can report it in
    #    the response. get_resume_info() does its own existence + event
    #    check, so the second call is cheap (it just re-reads the tail
    #    event of the same session). Catching the not-resumable case
    #    above already guarantees get_resume_info() returns non-None.
    info = await get_resume_info(session_id, storage)
    last_seq = info.last_seq if info is not None else 0

    # 4. Invoke the core resume logic.
    try:
        new_sid, _events = await resume_session(
            session_id,
            storage,
            new_session_id=new_session_id,
        )
    except ResumeNotSupportedError as exc:
        # Defense in depth: can_resume() said yes above, but the core
        # raised (e.g. the session's events were deleted between our
        # check and the core's check). Surface the same 409 status so
        # callers can treat both paths uniformly.
        logger.warning(
            "resume_rejected_by_core",
            original_session=session_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        )

    # 5. Fetch the new session's current status. The core creates the
    #    new session in PENDING, then runs the executor synchronously;
    #    by the time we get here the new session may be in any
    #    terminal state. Report whatever storage shows.
    new_session = await storage.get_session(new_sid)
    new_status = (
        new_session.status.value
        if new_session is not None
        else SessionStatus.PENDING.value
    )

    logger.info(
        "session_resumed",
        original_session=session_id,
        new_session=new_sid,
        last_seq=last_seq,
        new_status=new_status,
    )

    return ResumeResponse(
        success=True,
        original_session_id=session_id,
        new_session_id=new_sid,
        last_seq=last_seq,
        status=new_status,
        message=f"Resumed session {session_id} to {new_sid}",
    )
