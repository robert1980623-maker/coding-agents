"""Tag management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from coding_agents.http.auth import verify_token
from coding_agents.storage.sqlite import SQLiteStorage

router = APIRouter(prefix="/sessions/{session_id}/tags", tags=["tags"])


class TagRequest(BaseModel):
    """Request body for adding a tag."""

    tag: str = Field(..., description="Tag name")


class TagResponse(BaseModel):
    """Response for tag operations."""

    session_id: str
    tag: str
    message: str


class TagsListResponse(BaseModel):
    """Response for listing tags."""

    session_id: str
    tags: list[str]


@router.post("", response_model=TagResponse, status_code=status.HTTP_201_CREATED)
async def add_tag(
    session_id: str,
    request: TagRequest,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> TagResponse:
    """Add a tag to a session."""
    # Verify session exists
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    await storage.add_tag(session_id, request.tag)

    return TagResponse(
        session_id=session_id,
        tag=request.tag,
        message=f"Added tag '{request.tag}' to session {session_id}",
    )


@router.delete("/{tag}", response_model=TagResponse)
async def remove_tag(
    session_id: str,
    tag: str,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> TagResponse:
    """Remove a tag from a session."""
    # Verify session exists
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    await storage.remove_tag(session_id, tag)

    return TagResponse(
        session_id=session_id,
        tag=tag,
        message=f"Removed tag '{tag}' from session {session_id}",
    )


@router.get("", response_model=TagsListResponse)
async def list_tags(
    session_id: str,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> TagsListResponse:
    """List all tags for a session."""
    # Verify session exists
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    tags = await storage.list_tags(session_id)

    return TagsListResponse(session_id=session_id, tags=tags)
