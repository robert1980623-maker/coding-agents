"""Tests for ConcurrencyGate."""

from __future__ import annotations

import asyncio

import pytest

from coding_agents.rate_limit.concurrency import ConcurrencyGate


class TestConcurrencyGate:
    """Test ConcurrencyGate async context manager."""

    async def test_basic_acquire_release(self):
        """Should track active count through acquire/release cycle."""
        gate = ConcurrencyGate(max_concurrent=3)
        assert gate.metrics == {"active": 0, "peak": 0}

        async with gate:
            assert gate.metrics["active"] == 1
            assert gate.metrics["peak"] == 1

        assert gate.metrics["active"] == 0
        assert gate.metrics["peak"] == 1

    async def test_peak_concurrency(self):
        """Should track the peak concurrent usage."""
        gate = ConcurrencyGate(max_concurrent=5)

        async with gate:
            async with gate:
                async with gate:
                    assert gate.metrics["active"] == 3
                    assert gate.metrics["peak"] == 3
                assert gate.metrics["active"] == 2
            assert gate.metrics["active"] == 1
        assert gate.metrics["active"] == 0
        assert gate.metrics["peak"] == 3

    async def test_limits_concurrency(self):
        """Should block when the concurrency limit is reached."""
        gate = ConcurrencyGate(max_concurrent=2)
        acquired: list[int] = []
        max_concurrent = 0
        lock = asyncio.Lock()

        async def worker(index: int) -> None:
            nonlocal max_concurrent
            async with gate:
                async with lock:
                    acquired.append(index)
                    if len(acquired) > max_concurrent:
                        max_concurrent = len(acquired)
                await asyncio.sleep(0.05)
                async with lock:
                    acquired.remove(index)

        await asyncio.gather(*(worker(i) for i in range(5)))
        assert max_concurrent <= 2

    async def test_peak_tracks_highest_ever(self):
        """Peak should not decrease when tasks complete."""
        gate = ConcurrencyGate(max_concurrent=3)

        async with gate:
            async with gate:
                async with gate:
                    pass  # peak = 3

        assert gate.metrics["peak"] == 3
        assert gate.metrics["active"] == 0

        async with gate:
            assert gate.metrics["peak"] == 3  # still 3, not 1
            assert gate.metrics["active"] == 1

    async def test_returns_self(self):
        """__aenter__ should return the gate instance."""
        gate = ConcurrencyGate(max_concurrent=2)
        async with gate as g:
            assert g is gate

    async def test_exception_in_context(self):
        """Should release the gate even when the body raises."""
        gate = ConcurrencyGate(max_concurrent=2)

        with pytest.raises(ValueError):
            async with gate:
                assert gate.metrics["active"] == 1
                raise ValueError("boom")

        # Gate must be released after the exception.
        assert gate.metrics["active"] == 0
        assert gate.metrics["peak"] == 1

    async def test_metrics_initial_state(self):
        """Initial metrics should be zero."""
        gate = ConcurrencyGate(max_concurrent=5)
        assert gate.metrics == {"active": 0, "peak": 0}

    async def test_peak_concurrency_with_staggered_release(self):
        """Peak should reflect concurrent usage with staggered releases."""
        gate = ConcurrencyGate(max_concurrent=3)
        acquired_count = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def worker(hold_time: float) -> None:
            nonlocal acquired_count, max_seen
            async with gate:
                async with lock:
                    acquired_count += 1
                    if acquired_count > max_seen:
                        max_seen = acquired_count
                await asyncio.sleep(hold_time)
                async with lock:
                    acquired_count -= 1

        # 4 tasks, max_concurrent=3; staggered so 3 are in-flight at once.
        await asyncio.gather(
            worker(0.05),
            worker(0.05),
            worker(0.05),
            worker(0.05),
        )
        assert max_seen == 3
        assert gate.metrics["peak"] == 3

    def test_max_concurrent_zero_raises(self):
        """max_concurrent=0 should raise ValueError."""
        with pytest.raises(ValueError, match="max_concurrent"):
            ConcurrencyGate(max_concurrent=0)

    def test_max_concurrent_negative_raises(self):
        """Negative max_concurrent should raise ValueError."""
        with pytest.raises(ValueError, match="max_concurrent"):
            ConcurrencyGate(max_concurrent=-1)

    def test_max_concurrent_one_allowed(self):
        """max_concurrent=1 is the minimum valid value."""
        gate = ConcurrencyGate(max_concurrent=1)
        assert gate.max_concurrent == 1
