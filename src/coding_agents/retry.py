"""Retry mechanism for session execution.

Provides exponential backoff retry logic for transient failures.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional, Type, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")


class RetryError(Exception):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, message: str, last_exception: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.last_exception = last_exception


@dataclass
class RetryPolicy:
    """Configuration for retry behavior.

    Attributes:
        max_retries: Maximum number of retry attempts (0 = no retry)
        delay_seconds: Initial delay between retries
        backoff_multiplier: Multiplier for exponential backoff
        retry_on: Tuple of exception types to retry on
    """

    max_retries: int = 3
    delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    retry_on: tuple[Type[Exception], ...] = (Exception,)

    def should_retry(self, exception: Exception) -> bool:
        """Check if the exception should trigger a retry."""
        return isinstance(exception, self.retry_on)

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for the given attempt (0-indexed).

        Uses exponential backoff: delay * (backoff_multiplier ** attempt)
        """
        return self.delay_seconds * (self.backoff_multiplier ** attempt)


async def with_retry(
    coro_factory: Callable[[], Any],
    policy: RetryPolicy,
    operation_name: str = "operation",
) -> Any:
    """Execute an async operation with retry logic.

    Args:
        coro_factory: Callable that returns a new coroutine/awaitable on each call.
                     Must create a fresh coroutine each time (can't reuse).
        policy: RetryPolicy configuration
        operation_name: Name for logging purposes

    Returns:
        The result of the successful coroutine

    Raises:
        RetryError: When all retry attempts are exhausted
        Exception: The original exception if it doesn't match retry_on
    """
    last_exception: Optional[Exception] = None

    for attempt in range(policy.max_retries + 1):
        try:
            result = await coro_factory()
            if attempt > 0:
                logger.info(
                    "retry_success",
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_retries=policy.max_retries,
                )
            return result
        except Exception as e:
            last_exception = e

            if attempt < policy.max_retries and policy.should_retry(e):
                delay = policy.get_delay(attempt)
                logger.warning(
                    "retry_attempt",
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_retries=policy.max_retries,
                    delay_seconds=delay,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                await asyncio.sleep(delay)
            else:
                # Don't retry: either exhausted or exception doesn't match
                break

    # All retries exhausted or exception doesn't match retry_on
    # If exception doesn't match, re-raise it directly
    if last_exception is not None and not policy.should_retry(last_exception):
        raise last_exception

    # Otherwise, raise RetryError
    error_msg = f"{operation_name} failed after {policy.max_retries + 1} attempts"
    logger.error(
        "retry_exhausted",
        operation=operation_name,
        attempts=policy.max_retries + 1,
        error=str(last_exception),
        error_type=type(last_exception).__name__ if last_exception else None,
    )
    raise RetryError(error_msg, last_exception=last_exception)


async def with_retry_generator(
    gen_factory: Callable[[], AsyncIterator[T]],
    policy: RetryPolicy,
    operation_name: str = "operation",
) -> AsyncIterator[T]:
    """Execute an async generator with retry logic.

    Unlike with_retry, this handles async generators (AsyncIterator).
    On failure, it restarts the entire generator from the beginning.

    Args:
        gen_factory: Callable that returns a new async generator on each call.
                    Must create a fresh generator each time.
        policy: RetryPolicy configuration
        operation_name: Name for logging purposes

    Yields:
        Items from the successful generator

    Raises:
        RetryError: When all retry attempts are exhausted
    """
    last_exception: Optional[Exception] = None

    for attempt in range(policy.max_retries + 1):
        try:
            gen = gen_factory()
            async for item in gen:
                yield item

            # Generator completed successfully
            if attempt > 0:
                logger.info(
                    "retry_success",
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_retries=policy.max_retries,
                )
            return
        except Exception as e:
            last_exception = e

            if attempt < policy.max_retries and policy.should_retry(e):
                delay = policy.get_delay(attempt)
                logger.warning(
                    "retry_attempt",
                    operation=operation_name,
                    attempt=attempt + 1,
                    max_retries=policy.max_retries,
                    delay_seconds=delay,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                await asyncio.sleep(delay)
            else:
                break

    # All retries exhausted
    error_msg = f"{operation_name} failed after {policy.max_retries + 1} attempts"
    logger.error(
        "retry_exhausted",
        operation=operation_name,
        attempts=policy.max_retries + 1,
        error=str(last_exception),
        error_type=type(last_exception).__name__ if last_exception else None,
    )
    raise RetryError(error_msg, last_exception=last_exception)
