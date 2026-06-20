"""Retry integration for StreamExecutor.

Provides a wrapper that adds retry logic to executor.execute() without
modifying the executor.py source code.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import structlog

from coding_agents.executor import StreamExecutor
from coding_agents.models import Event
from coding_agents.retry import RetryPolicy, with_retry_generator

logger = structlog.get_logger(__name__)


def make_executor_with_retry(
    executor: StreamExecutor,
    policy: RetryPolicy,
) -> StreamExecutor:
    """Wrap executor.execute() with retry logic.

    Returns a new StreamExecutor-like object that retries execute() on failure.
    The original executor is not modified.

    Args:
        executor: The StreamExecutor to wrap
        policy: RetryPolicy configuration

    Returns:
        A wrapped executor with retry logic
    """

    class RetryingExecutor:
        """Proxy that adds retry to execute()."""

        def __init__(self, wrapped: StreamExecutor, retry_policy: RetryPolicy) -> None:
            self._wrapped = wrapped
            self._policy = retry_policy
            # Proxy other attributes
            self.store = wrapped.store
            self.config = wrapped.config

        async def execute(
            self,
            session_id: str,
            command: list[str],
            workdir: str,
            env: dict[str, str] | None = None,
        ) -> AsyncIterator[Event]:
            """Execute with retry logic.

            On failure, restarts the entire execution from the beginning.
            """

            def factory():
                # Create a fresh generator each time
                return self._wrapped.execute(session_id, command, workdir, env)

            # Use with_retry_generator to handle the async generator
            async for event in with_retry_generator(
                factory, self._policy, operation_name=f"execute({session_id})"
            ):
                yield event

        async def kill(self) -> None:
            """Proxy kill to wrapped executor."""
            await self._wrapped.kill()

    return RetryingExecutor(executor, policy)
