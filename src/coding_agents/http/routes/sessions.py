"""Session management routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from coding_agents.http.auth import verify_token
from coding_agents.models import AgentType, Session, SessionStatus
from coding_agents.storage.sqlite import SQLiteStorage

router = APIRouter(prefix="/sessions", tags=["sessions"])


# Pydantic models for request/response
class CreateSessionRequest(BaseModel):
    """Request body for creating a new session."""

    agent: AgentType = Field(default=AgentType.CLAUDE, description="Agent type")
    prompt: str = Field(default="", description="Prompt to execute")
    workdir: str = Field(default=".", description="Working directory")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


class SessionResponse(BaseModel):
    """Response model for a session."""

    id: str
    agent: str
    prompt: str
    workdir: str
    status: str
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    last_heartbeat_at: Optional[str] = None
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


def session_to_response(session: Session) -> SessionResponse:
    """Convert a Session dataclass to a SessionResponse Pydantic model."""
    return SessionResponse(
        id=session.id,
        agent=session.agent.value,
        prompt=session.prompt,
        workdir=session.workdir,
        status=session.status.value,
        pid=session.pid,
        exit_code=session.exit_code,
        started_at=session.started_at.isoformat() if session.started_at else None,
        finished_at=session.finished_at.isoformat() if session.finished_at else None,
        duration_ms=session.duration_ms,
        last_heartbeat_at=session.last_heartbeat_at.isoformat() if session.last_heartbeat_at else None,
        cost_usd=session.cost_usd,
        input_tokens=session.input_tokens,
        output_tokens=session.output_tokens,
        cache_read_tokens=session.cache_read_tokens,
        cache_write_tokens=session.cache_write_tokens,
        model=session.model,
        provider=session.provider,
        metadata=session.metadata,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
    )


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    request: CreateSessionRequest,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> SessionResponse:
    """Create a new session.

    This creates a session record in PENDING status. The actual execution
    must be triggered separately (not yet implemented in HTTP API).
    """
    session = Session(
        agent=request.agent,
        prompt=request.prompt,
        workdir=request.workdir,
        metadata=request.metadata,
    )

    session_id = await storage.create_session(session)
    created_session = await storage.get_session(session_id)

    if created_session is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create session",
        )

    return session_to_response(created_session)


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    agent: Optional[str] = Query(None, description="Filter by agent type"),
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status"
    ),
    tag: Optional[list[str]] = Query(None, description="Filter by tags"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of sessions"),
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> list[SessionResponse]:
    """List sessions with optional filters."""
    sessions = await storage.list_sessions(
        agent=agent,
        status=status_filter,
        tags=tag,
        limit=limit,
    )

    return [session_to_response(s) for s in sessions]


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> SessionResponse:
    """Get a specific session by ID."""
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    return session_to_response(session)
