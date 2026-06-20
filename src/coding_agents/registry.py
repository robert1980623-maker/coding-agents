"""SessionRegistry — concurrency control for agent executions."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


class SessionRegistry:
    """Session registry with semaphore-based concurrency control.

    P0-NEW fixes (v1.2.1):
    - P0-NEW-1: kill_session releases semaphore slot (prevents deadlock after 5 kills)
    - P0-NEW-2: acquire() checks for duplicate acquire of same session_id (prevents slot leak)
    """

    def __init__(self, max_concurrent: int = 5):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_sessions: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()
        self._acquired: set[str] = set()
        self._max_concurrent = max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def available_slots(self) -> int:
        return self._semaphore._value

    async def acquire(self, session_id: str, timeout: float = 60.0) -> bool:
        """Acquire an execution slot for a session.

        Returns True on success, False if timeout waiting for a slot.
        Raises RuntimeError if session_id is already acquired.
        """
        async with self._lock:
            if session_id in self._acquired:
                raise RuntimeError(f"session {session_id} already acquired")

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return False

        # Record after semaphore acquired; re-check under lock to close the
        # race window between the initial check and the semaphore wait.
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("no current task")
        async with self._lock:
            if session_id in self._acquired:
                # Another coroutine acquired it while we waited for the semaphore.
                self._semaphore.release()
                raise RuntimeError(f"session {session_id} already acquired")
            self._active_sessions[session_id] = task
            self._acquired.add(session_id)
        return True

    async def release(self, session_id: str) -> None:
        """Release the execution slot for a session."""
        async with self._lock:
            self._active_sessions.pop(session_id, None)
            had_slot = session_id in self._acquired
            self._acquired.discard(session_id)
        # Only release semaphore if we actually held a slot
        if had_slot:
            self._semaphore.release()

    async def list_active(self) -> list[str]:
        """List all currently-active session IDs."""
        async with self._lock:
            return list(self._active_sessions.keys())

    async def kill_session(self, session_id: str) -> bool:
        """Cancel the task for a session and release its slot.

        Returns True if a task was found and cancelled.
        P0-NEW-1: This also releases the semaphore slot so subsequent
        sessions can proceed. Without this, 5 kills would deadlock.
        """
        async with self._lock:
            task = self._active_sessions.get(session_id)
            had_slot = session_id in self._acquired

        if task is not None and not task.done():
            task.cancel()

        if had_slot:
            async with self._lock:
                self._active_sessions.pop(session_id, None)
                self._acquired.discard(session_id)
            self._semaphore.release()

        return task is not None and not task.done()
