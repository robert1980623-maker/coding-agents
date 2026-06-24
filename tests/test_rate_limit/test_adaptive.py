"""Tests for AdaptiveRateLimiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from coding_agents.rate_limit.adaptive import AdaptiveRateLimiter


class TestAdaptiveRateLimiter:
    """Test AdaptiveRateLimiter token bucket."""

    async def test_initial_rate(self):
        """Should start with the configured initial rate."""
        limiter = AdaptiveRateLimiter(initial_rate=5.0)
        assert limiter.current_rate == 5.0

    async def test_initial_tokens(self):
        """Should start with tokens equal to initial_rate (one second of tokens)."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        assert limiter.tokens >= 9.9  # allow for small elapsed time

    async def test_acquire_with_available_tokens(self):
        """Should acquire immediately when tokens are available."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 0.1  # should be near-instant

    async def test_acquire_decrements_tokens(self):
        """Each acquire should consume one token."""
        limiter = AdaptiveRateLimiter(initial_rate=100.0, max_rate=100.0)
        initial_tokens = limiter.tokens
        await limiter.acquire()
        # Allow a small amount of refill due to elapsed time.
        assert limiter.tokens <= initial_tokens

    async def test_acquire_waits_when_no_tokens(self):
        """Should wait when tokens are exhausted."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0, max_rate=10.0)
        # Drain all tokens.
        limiter._tokens = 0.0

        start = time.monotonic()
        await limiter.acquire()
        elapsed = time.monotonic() - start

        # At rate=10/s, one token takes 0.1s.
        assert elapsed >= 0.05
        assert elapsed < 0.5

    async def test_report_429_halves_rate(self):
        """report_429 should halve the current rate."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0, min_rate=0.1)
        assert limiter.current_rate == 10.0

        limiter.report_429()
        assert limiter.current_rate == 5.0

        limiter.report_429()
        assert limiter.current_rate == 2.5

    async def test_report_429_respects_min_rate(self):
        """Rate should not drop below min_rate."""
        limiter = AdaptiveRateLimiter(initial_rate=1.0, min_rate=0.5)

        limiter.report_429()
        assert limiter.current_rate == 0.5

        limiter.report_429()
        assert limiter.current_rate == 0.5  # clamped

    async def test_report_success_increases_rate(self):
        """report_success should increase rate by 1 %."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0, max_rate=100.0)

        limiter.report_success()
        assert abs(limiter.current_rate - 10.1) < 0.001

    async def test_report_success_respects_max_rate(self):
        """Rate should not exceed max_rate."""
        limiter = AdaptiveRateLimiter(initial_rate=99.0, max_rate=100.0)

        limiter.report_success()
        assert abs(limiter.current_rate - 99.99) < 0.001

        limiter.report_success()
        assert limiter.current_rate == 100.0  # clamped

    async def test_metrics_initial(self):
        """Initial metrics should reflect starting state."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        m = limiter.metrics
        assert m["current_rate"] == 10.0
        assert m["total_acquired"] == 0
        assert m["total_429s"] == 0

    async def test_metrics_after_acquire(self):
        """total_acquired should increment after each acquire."""
        limiter = AdaptiveRateLimiter(initial_rate=100.0, max_rate=100.0)

        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()

        assert limiter.metrics["total_acquired"] == 3

    async def test_metrics_after_429(self):
        """total_429s should increment after each report_429."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0)

        limiter.report_429()
        limiter.report_429()
        assert limiter.metrics["total_429s"] == 2

    async def test_adaptation_under_429_storm(self):
        """Rate should decay exponentially under sustained 429s."""
        limiter = AdaptiveRateLimiter(initial_rate=100.0, min_rate=0.1)
        for _ in range(10):
            limiter.report_429()
        # 100 → 50 → 25 → 12.5 → 6.25 → 3.125 → 1.5625 → 0.78125 → 0.390625 → 0.1953125 → 0.1
        assert limiter.current_rate == pytest.approx(0.1, abs=0.01)

    async def test_recovery_after_429(self):
        """Rate should slowly recover after 429s via report_success."""
        limiter = AdaptiveRateLimiter(initial_rate=10.0, max_rate=100.0)
        limiter.report_429()  # rate = 5.0

        # 100 successful requests → 5.0 * 1.01^100 ≈ 13.5
        for _ in range(100):
            limiter.report_success()
        assert limiter.current_rate > 13.0
        assert limiter.current_rate < 14.0

    async def test_concurrent_acquire(self):
        """Multiple concurrent acquires should all succeed."""
        limiter = AdaptiveRateLimiter(initial_rate=1000.0, max_rate=1000.0)

        results = await asyncio.gather(*(limiter.acquire() for _ in range(10)))
        assert limiter.metrics["total_acquired"] == 10

    async def test_initial_rate_zero_raises(self):
        """initial_rate=0 should raise ValueError (avoid divide-by-zero)."""
        with pytest.raises(ValueError, match="initial_rate"):
            AdaptiveRateLimiter(initial_rate=0.0)

    async def test_initial_rate_negative_raises(self):
        """Negative initial_rate should raise ValueError."""
        with pytest.raises(ValueError, match="initial_rate"):
            AdaptiveRateLimiter(initial_rate=-1.0)

    async def test_min_rate_zero_raises(self):
        """min_rate=0 should raise ValueError."""
        with pytest.raises(ValueError, match="min_rate"):
            AdaptiveRateLimiter(min_rate=0.0)

    async def test_max_rate_below_min_raises(self):
        """max_rate < min_rate should raise ValueError."""
        with pytest.raises(ValueError, match="max_rate"):
            AdaptiveRateLimiter(min_rate=5.0, max_rate=1.0)

    async def test_acquire_cancellation_releases_lock(self):
        """Cancelling acquire() during the wait must not deadlock the lock.

        After cancellation the lock should be free so subsequent acquires
        succeed immediately.
        """
        limiter = AdaptiveRateLimiter(initial_rate=1.0, max_rate=1.0)
        # Drain tokens so acquire() has to wait.
        limiter._tokens = 0.0

        task = asyncio.create_task(limiter.acquire())
        # Give the task a chance to enter the wait and release the lock.
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The lock must be available again; otherwise this would deadlock.
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)

    async def test_no_divide_by_zero_with_tiny_rate(self):
        """Acquire must not ZeroDivisionError even with a very small rate."""
        limiter = AdaptiveRateLimiter(initial_rate=0.1, min_rate=0.1)
        limiter._current_rate = 0.1
        limiter._tokens = 0.0
        # Should wait ~10s for one token at rate=0.1, but we timeout quickly.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire(), timeout=0.2)
