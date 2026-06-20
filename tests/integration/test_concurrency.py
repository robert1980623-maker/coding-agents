"""Concurrency stress tests for SessionRegistry.

Tests verify that the semaphore-based concurrency control works correctly
under load: slot acquisition, release, queueing, and leak detection.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from coding_agents.registry import SessionRegistry


class TestConcurrentSessions:
    """Concurrency control tests for SessionRegistry."""

    async def test_basic_acquire_release(self):
        """Single acquire/release cycle."""
        registry = SessionRegistry(max_concurrent=5)

        acquired = await registry.acquire("sess-1")
        assert acquired is True
        assert registry.available_slots == 4

        active = await registry.list_active()
        assert "sess-1" in active

        await registry.release("sess-1")
        assert registry.available_slots == 5

        active = await registry.list_active()
        assert "sess-1" not in active
        assert len(active) == 0

    async def test_concurrent_5_sessions(self):
        """5 concurrent sessions with max_concurrent=5 — no queueing."""
        registry = SessionRegistry(max_concurrent=5)

        async def run_one(i: int):
            acquired = await registry.acquire(f"sess-{i}")
            assert acquired is True
            await asyncio.sleep(0.2)
            await registry.release(f"sess-{i}")

        tasks = [run_one(i) for i in range(5)]
        start = time.monotonic()
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        # All 5 should run concurrently — total time ≈ 0.2s (not 1.0s)
        assert elapsed < 0.8, (
            f"5 concurrent sessions took {elapsed:.2f}s — expected < 0.8s"
        )

        active = await registry.list_active()
        assert len(active) == 0, f"Leaked active sessions: {active}"
        assert registry.available_slots == 5

    async def test_queueing_when_over_capacity(self):
        """10 sessions with max_concurrent=5 — queueing must occur."""
        registry = SessionRegistry(max_concurrent=5)

        async def run_one(i: int):
            acquired = await registry.acquire(f"sess-{i}")
            assert acquired is True
            await asyncio.sleep(0.5)
            await registry.release(f"sess-{i}")

        tasks = [run_one(i) for i in range(10)]
        start = time.monotonic()
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        # 10 tasks / 5 slots × 0.5s = ~1.0s minimum
        assert elapsed >= 0.9, (
            f"Expected >= 0.9s with queueing, got {elapsed:.2f}s"
        )
        # Should not take more than ~2.5s (allowing for scheduling overhead)
        assert elapsed < 2.5, (
            f"Expected < 2.5s, got {elapsed:.2f}s — possible slot leak"
        )

        print(f"\n[concurrency] 10 sessions / 5 slots: {elapsed:.2f}s")

        # All slots must be released
        active = await registry.list_active()
        assert len(active) == 0, f"Leaked active sessions: {active}"
        assert registry.available_slots == 5

    async def test_no_semaphore_leak_after_many_cycles(self):
        """Run 20 acquire/release cycles and verify no slot leak."""
        registry = SessionRegistry(max_concurrent=3)

        for i in range(20):
            acquired = await registry.acquire(f"sess-{i}")
            assert acquired is True, f"Failed to acquire at cycle {i}"
            await asyncio.sleep(0.01)
            await registry.release(f"sess-{i}")

        assert registry.available_slots == 3, (
            f"Semaphore leak: {registry.available_slots} slots available, expected 3"
        )
        active = await registry.list_active()
        assert len(active) == 0

    async def test_concurrent_burst_with_various_durations(self):
        """Burst of sessions with varying sleep times."""
        registry = SessionRegistry(max_concurrent=3)
        results: list[float] = []

        async def run_one(i: int):
            start = await registry.acquire(f"sess-{i}")
            assert start is True
            sleep_time = 0.1 * (i % 3 + 1)  # 0.1, 0.2, 0.3
            await asyncio.sleep(sleep_time)
            await registry.release(f"sess-{i}")
            return sleep_time

        tasks = [run_one(i) for i in range(9)]
        start = time.monotonic()
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        # 9 tasks / 3 slots = 3 batches
        # Each batch has tasks with sleeps 0.1, 0.2, 0.3
        # So each batch takes ~0.3s, total ~0.9s
        assert elapsed >= 0.5, f"Too fast ({elapsed:.2f}s) — concurrency limit not working?"
        assert elapsed < 3.0, f"Too slow ({elapsed:.2f}s) — possible slot leak"

        active = await registry.list_active()
        assert len(active) == 0
        assert registry.available_slots == 3

        print(f"\n[concurrency] 9 sessions / 3 slots (varied durations): {elapsed:.2f}s")

    async def test_duplicate_acquire_raises(self):
        """Acquiring the same session_id twice raises RuntimeError."""
        registry = SessionRegistry(max_concurrent=5)

        await registry.acquire("sess-dup")

        with pytest.raises(RuntimeError, match="already acquired"):
            await registry.acquire("sess-dup")

        await registry.release("sess-dup")

    async def test_acquire_timeout(self):
        """Acquire with a short timeout when slots are full."""
        registry = SessionRegistry(max_concurrent=1)

        # Fill the only slot
        await registry.acquire("sess-blocker")

        # Try to acquire another — should timeout
        start = time.monotonic()
        result = await registry.acquire("sess-waiter", timeout=0.3)
        elapsed = time.monotonic() - start

        assert result is False, "Expected acquire to fail with timeout"
        assert elapsed >= 0.25, f"Timeout returned too fast: {elapsed:.2f}s"
        assert elapsed < 1.0, f"Timeout took too long: {elapsed:.2f}s"

        # Clean up
        await registry.release("sess-blocker")
        assert registry.available_slots == 1

    async def test_release_without_acquire(self):
        """Releasing a session that was never acquired is a no-op."""
        registry = SessionRegistry(max_concurrent=5)
        initial_slots = registry.available_slots

        await registry.release("sess-nonexistent")

        assert registry.available_slots == initial_slots
        active = await registry.list_active()
        assert "sess-nonexistent" not in active
