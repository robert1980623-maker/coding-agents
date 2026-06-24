"""Concurrency limiting gate using asyncio semaphore.

Provides an async context manager that limits the number of concurrent
operations and tracks peak/active counts for metrics.
"""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


class ConcurrencyGate:
    """Limits concurrent async operations via semaphore.

    Tracks the current number of active operations and the historical peak
    for observability. Intended to be used as an async context manager
    around the protected operation.

    Attributes:
        max_concurrent: Maximum number of simultaneous operations allowed.
    """

    def __init__(self, max_concurrent: int = 3) -> None:
        """Initialize the concurrency gate.

        Args:
            max_concurrent: Maximum number of simultaneous operations allowed.
                Must be at least 1.

        Raises:
            ValueError: If ``max_concurrent`` is less than 1.
        """
        if max_concurrent < 1:
            raise ValueError(
                f"max_concurrent must be >= 1, got {max_concurrent}"
            )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: int = 0
        self._peak: int = 0
        self.max_concurrent: int = max_concurrent

    async def __aenter__(self) -> ConcurrencyGate:
        """Acquire the gate, blocking if the concurrency limit is reached."""
        await self._semaphore.acquire()
        self._active += 1
        if self._active > self._peak:
            self._peak = self._active
        logger.debug(
            "gate_acquired",
            active=self._active,
            peak=self._peak,
            max_concurrent=self.max_concurrent,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Release the gate, decrementing the active count."""
        self._active -= 1
        self._semaphore.release()
        logger.debug(
            "gate_released",
            active=self._active,
            peak=self._peak,
        )

    @property
    def metrics(self) -> dict:
        """Return a snapshot of the gate's current metrics.

        Returns:
            Dictionary with ``active`` (current in-flight count) and ``peak``
            (highest concurrent count observed since construction).
        """
        return {"active": self._active, "peak": self._peak}
