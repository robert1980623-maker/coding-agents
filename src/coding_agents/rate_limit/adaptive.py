"""Adaptive token-bucket rate limiter.

Implements a token bucket whose refill rate adapts to observed 429
responses: the rate is halved on each 429 signal and increased by 1 %
on each successful operation, clamped to configurable bounds.
"""

from __future__ import annotations

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


class AdaptiveRateLimiter:
    """Token bucket that adapts based on 429 feedback.

    The bucket refills continuously at ``current_rate`` tokens per second.
    Callers :meth:`acquire` one token at a time; if none are available the
    call awaits until one is.

    * :meth:`report_429` halves the rate (multiplicative decrease).
    * :meth:`report_success` multiplies the rate by 1.01 (additive-like
      increase).

    The rate is clamped to ``[min_rate, max_rate]`` at all times.

    Attributes:
        initial_rate: Starting request rate (tokens per second).
        min_rate: Lower bound on the adaptive rate.
        max_rate: Upper bound on the adaptive rate.
    """

    def __init__(
        self,
        initial_rate: float = 10.0,
        min_rate: float = 0.1,
        max_rate: float = 100.0,
    ) -> None:
        """Initialize the adaptive rate limiter.

        Args:
            initial_rate: Starting request rate (tokens per second). Must
                be strictly positive.
            min_rate: Lower bound on the adaptive rate. Must be strictly
                positive.
            max_rate: Upper bound on the adaptive rate. Must be at least
                ``min_rate``.

        Raises:
            ValueError: If the rate arguments are out of valid range.
        """
        if initial_rate <= 0:
            raise ValueError(
                f"initial_rate must be > 0, got {initial_rate}"
            )
        if min_rate <= 0:
            raise ValueError(f"min_rate must be > 0, got {min_rate}")
        if max_rate < min_rate:
            raise ValueError(
                f"max_rate ({max_rate}) must be >= min_rate ({min_rate})"
            )
        self.initial_rate: float = initial_rate
        self.min_rate: float = min_rate
        self.max_rate: float = max_rate
        self._current_rate: float = initial_rate
        self._tokens: float = initial_rate  # start with one second of tokens
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()
        self._total_acquired: int = 0
        self._total_429s: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Add tokens accumulated since the last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.max_rate,
                self._tokens + elapsed * self._current_rate,
            )
            self._last_refill = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def acquire(self) -> None:
        """Acquire one token, waiting if necessary.

        If no token is available the call sleeps for the exact time until
        the next token becomes available, then re-checks.
        """
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._total_acquired += 1
                    return

                # Guard against a zero rate (e.g. due to rounding / a
                # misconfiguration that slipped past validation) so we
                # never hit ZeroDivisionError here.
                wait_time = (1.0 - self._tokens) / max(self._current_rate, 1e-12)
                logger.debug(
                    "rate_limit_wait",
                    wait_seconds=round(wait_time, 4),
                    current_rate=round(self._current_rate, 4),
                )
                # Release the lock while sleeping so other coroutines
                # can call report_* without blocking. The finally block
                # guarantees the lock is re-acquired even if the sleep
                # is interrupted by a CancelledError — asyncio tasks
                # must complete finally blocks before the cancellation
                # propagates, so the lock remains consistent.
                self._lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await self._lock.acquire()

    def report_429(self) -> None:
        """Report a 429 response, halving the current rate.

        The rate is clamped to ``min_rate`` and the token bucket is reset
        to the new rate to avoid over-shooting on the next acquire.

        Note:
            This method mutates internal state without holding the lock.
            That is safe under asyncio's single-threaded cooperative
            scheduling: state transitions happen between await points,
            and no await occurs inside this method, so no other
            coroutine can interleave with it.
        """
        self._total_429s += 1
        old_rate = self._current_rate
        self._current_rate = max(self.min_rate, self._current_rate / 2.0)
        # Reset bucket to the new rate to avoid stale token buildup.
        self._tokens = min(self._tokens, self._current_rate)
        logger.warning(
            "rate_limit_429",
            old_rate=round(old_rate, 4),
            new_rate=round(self._current_rate, 4),
            total_429s=self._total_429s,
        )

    def report_success(self) -> None:
        """Report a successful request, increasing the rate by 1 %.

        The rate is clamped to ``max_rate``.

        Note:
            See :meth:`report_429` for thread-safety notes — this method
            is safe for the same reasons (no awaits, single-threaded
            asyncio event loop).
        """
        old_rate = self._current_rate
        self._current_rate = min(self.max_rate, self._current_rate * 1.01)
        logger.debug(
            "rate_limit_success",
            old_rate=round(old_rate, 4),
            new_rate=round(self._current_rate, 4),
        )

    @property
    def current_rate(self) -> float:
        """Current token refill rate (tokens per second)."""
        return self._current_rate

    @property
    def tokens(self) -> float:
        """Current number of available tokens (refilled lazily)."""
        self._refill()
        return self._tokens

    @property
    def metrics(self) -> dict:
        """Return a snapshot of the limiter's metrics.

        Returns:
            Dictionary with ``current_rate``, ``tokens``, ``total_acquired``,
            and ``total_429s``.
        """
        return {
            "current_rate": self._current_rate,
            "tokens": self._tokens,
            "total_acquired": self._total_acquired,
            "total_429s": self._total_429s,
        }
