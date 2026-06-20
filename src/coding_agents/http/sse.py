"""Server-Sent Events (SSE) formatting for event streaming."""

from __future__ import annotations

import json
from typing import Any

from coding_agents.models import Event


def format_event_as_sse(event: Event) -> dict[str, Any]:
    """Format an Event as an SSE data structure.

    Args:
        event: The event to format.

    Returns:
        A dict with 'event', 'id', and 'data' keys for SSE formatting.
    """
    data = {
        "id": event.id,
        "session_id": event.session_id,
        "channel": event.channel,
        "seq": event.seq,
        "type": event.type.value,
        "data": event.data,
        "created_at": event.created_at.isoformat(),
    }

    return {
        "event": event.type.value,
        "id": str(event.id) if event.id is not None else str(event.seq),
        "data": json.dumps(data),
    }


def parse_last_event_id(last_event_id: str | None) -> int:
    """Parse the Last-Event-ID header to determine where to resume streaming.

    Args:
        last_event_id: The Last-Event-ID header value (event ID or seq number).

    Returns:
        The sequence number to resume after (events with seq > this value).
    """
    if last_event_id is None:
        return 0

    try:
        # Try to parse as integer (event ID or seq)
        return int(last_event_id)
    except (ValueError, TypeError):
        # If invalid, start from beginning
        return 0
