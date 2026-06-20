"""Event retrieval routes (REST and SSE)."""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from coding_agents.http.auth import verify_token
from coding_agents.http.sse import format_event_as_sse, parse_last_event_id
from coding_agents.models import Event
from coding_agents.storage.sqlite import SQLiteStorage

router = APIRouter(prefix="/sessions/{session_id}/events", tags=["events"])


class EventResponse(BaseModel):
    """Response model for an event."""

    id: Optional[int] = None
    session_id: str
    channel: str
    seq: int
    type: str
    data: str
    raw_json: Optional[str] = None
    created_at: str
    metadata: dict[str, Any]


def event_to_response(event: Event) -> EventResponse:
    """Convert an Event dataclass to an EventResponse Pydantic model."""
    return EventResponse(
        id=event.id,
        session_id=event.session_id,
        channel=event.channel,
        seq=event.seq,
        type=event.type.value,
        data=event.data,
        raw_json=event.raw_json,
        created_at=event.created_at.isoformat(),
        metadata=event.metadata,
    )


@router.get("", response_model=list[EventResponse])
async def get_events(
    session_id: str,
    after_seq: int = Query(0, ge=0, description="Return events after this sequence number"),
    limit: Optional[int] = Query(None, ge=1, description="Maximum number of events"),
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> list[EventResponse]:
    """Get events for a session via REST API."""
    # Verify session exists
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    events = await storage.get_events(session_id, after_seq=after_seq, limit=limit)
    return [event_to_response(e) for e in events]


@router.get("/stream")
async def stream_events(
    session_id: str,
    request: Request,
    token: str = Depends(verify_token),
    storage: SQLiteStorage = Depends(),
) -> EventSourceResponse:
    """Stream events for a session via Server-Sent Events.

    Supports Last-Event-ID header for resuming after disconnection.
    """
    # Verify session exists
    session = await storage.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}",
        )

    # Parse Last-Event-ID header for resumption
    last_event_id = request.headers.get("Last-Event-ID")
    after_seq = parse_last_event_id(last_event_id)

    async def event_generator() -> AsyncIterator[dict[str, Any]]:
        """Generate SSE events from the storage stream."""
        async for event in storage.stream_events(session_id, after_seq=after_seq):
            if await request.is_disconnected():
                break
            yield format_event_as_sse(event)

    return EventSourceResponse(event_generator())
