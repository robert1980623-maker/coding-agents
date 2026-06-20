"""StorageBackend Protocol definition."""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional, Protocol, runtime_checkable

from coding_agents.models import Event, Session


@runtime_checkable
class StorageBackend(Protocol):
    """Storage backend protocol.

    All methods are async. Implementations must be safe for concurrent use
    within a single process.
    """

    async def initialize(self) -> None:
        """Initialize the storage backend (create tables, indexes, etc.)."""
        ...

    async def close(self) -> None:
        """Close any open connections."""
        ...

    # ---- Session operations ----

    async def create_session(self, session: Session) -> str:
        """Create a session, returning its id."""
        ...

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve a session by id."""
        ...

    async def update_session(self, session_id: str, **kwargs: Any) -> None:
        """Update fields on an existing session."""
        ...

    async def list_sessions(
        self,
        agent: Optional[str] = None,
        status: Optional[str] = None,
        tags: Optional[list[str]] = None,
        limit: int = 100,
    ) -> list[Session]:
        """List sessions with optional filters."""
        ...

    # ---- Tag operations ----

    async def add_tag(self, session_id: str, tag: str) -> None:
        """Add a tag to a session."""
        ...

    async def remove_tag(self, session_id: str, tag: str) -> None:
        """Remove a tag from a session."""
        ...

    async def list_tags(self, session_id: str) -> list[str]:
        """List all tags for a session."""
        ...

    # ---- Event operations ----

    async def append_events(self, events: list[Event]) -> None:
        """Append events to storage."""
        ...

    async def get_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: Optional[int] = None,
    ) -> list[Event]:
        """Get events for a session, optionally after a given seq."""
        ...

    def stream_events(
        self,
        session_id: str,
        after_seq: int = 0,
    ) -> AsyncIterator[Event]:
        """Stream events for a session."""
        ...

    async def search_events(
        self,
        query: str,
        agent: Optional[str] = None,
        limit: int = 20,
    ) -> list[Event]:
        """Full-text search across events using FTS5."""
        ...

    # ---- Recovery ----

    async def recover_orphaned_sessions(self, timeout_seconds: int = 300) -> int:
        """Scan and mark orphaned sessions. Returns the count marked."""
        ...
