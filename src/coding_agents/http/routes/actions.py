"""Action routes (kill, recover)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from coding_agents.http.auth import verify_token
from coding_agents.models import SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage

router = APIRouter(tags=["actions"])


class KillResponse(BaseModel):
    """Response for kill action."""

    success: bool
    session_id: str
    message: str


class RecoverResponse(BaseModel):
    """Response for recover action."""

    recovered_count: int
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
    timeout: int = 300,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> RecoverResponse:
    """Recover orphaned sessions.

    Marks sessions that have been running for longer than the timeout
    without a heartbeat as ORPHANED.
    """
    count = await storage.recover_orphaned_sessions(timeout_seconds=timeout)

    return RecoverResponse(
        recovered_count=count,
        message=f"Recovered {count} orphaned sessions",
    )
