"""Tests for RateLimitMetrics."""

from __future__ import annotations

import pytest

from coding_agents.rate_limit.adaptive import AdaptiveRateLimiter
from coding_agents.rate_limit.concurrency import ConcurrencyGate
from coding_agents.rate_limit.metrics import RateLimitMetrics


class TestRateLimitMetrics:
    """Test RateLimitMetrics aggregation."""

    def test_snapshot_initial_state(self):
        """Snapshot should reflect initial state of both components."""
        gate = ConcurrencyGate(max_concurrent=3)
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        metrics = RateLimitMetrics(gate, limiter)

        snapshot = metrics.snapshot()

        assert "concurrency" in snapshot
        assert "limiter" in snapshot
        assert snapshot["concurrency"] == {"active": 0, "peak": 0}
        assert snapshot["limiter"]["current_rate"] == 10.0
        assert snapshot["limiter"]["total_acquired"] == 0
        assert snapshot["limiter"]["total_429s"] == 0

    async def test_snapshot_after_operations(self):
        """Snapshot should reflect state after operations."""
        gate = ConcurrencyGate(max_concurrent=3)
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        metrics = RateLimitMetrics(gate, limiter)

        # Perform some operations
        async with gate:
            await limiter.acquire()
            await limiter.acquire()
            limiter.report_429()

        snapshot = metrics.snapshot()

        assert snapshot["concurrency"]["active"] == 0
        assert snapshot["concurrency"]["peak"] == 1
        assert snapshot["limiter"]["total_acquired"] == 2
        assert snapshot["limiter"]["total_429s"] == 1
        assert snapshot["limiter"]["current_rate"] < 10.0  # rate decreased

    async def test_snapshot_updates_dynamically(self):
        """Each snapshot call should reflect current state."""
        gate = ConcurrencyGate(max_concurrent=3)
        limiter = AdaptiveRateLimiter(initial_rate=10.0)
        metrics = RateLimitMetrics(gate, limiter)

        # First snapshot
        snap1 = metrics.snapshot()
        assert snap1["limiter"]["total_acquired"] == 0

        # Perform operation
        await limiter.acquire()

        # Second snapshot should show updated state
        snap2 = metrics.snapshot()
        assert snap2["limiter"]["total_acquired"] == 1

    async def test_snapshot_with_concurrent_operations(self):
        """Snapshot should handle concurrent operations correctly."""
        import asyncio

        gate = ConcurrencyGate(max_concurrent=5)
        limiter = AdaptiveRateLimiter(initial_rate=100.0, max_rate=100.0)
        metrics = RateLimitMetrics(gate, limiter)

        async def worker():
            async with gate:
                await limiter.acquire()
                # Hold the gate long enough for all 3 workers to overlap.
                await asyncio.sleep(0.05)

        await asyncio.gather(*(worker() for _ in range(3)))

        snapshot = metrics.snapshot()
        assert snapshot["concurrency"]["peak"] == 3
        assert snapshot["limiter"]["total_acquired"] == 3
