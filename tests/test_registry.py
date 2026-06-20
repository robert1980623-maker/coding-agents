"""Tests for SessionRegistry."""

from __future__ import annotations

import asyncio

import pytest

from coding_agents.registry import SessionRegistry


class TestAcquire:
    async def test_acquire_success(self):
        reg = SessionRegistry(max_concurrent=2)
        result = await reg.acquire("session-1")
        assert result is True

    async def test_acquire_releases_on_release(self):
        reg = SessionRegistry(max_concurrent=1)
        await reg.acquire("session-1")
        await reg.release("session-1")
        result = await reg.acquire("session-2")
        assert result is True

    async def test_acquire_timeout(self):
        reg = SessionRegistry(max_concurrent=1)
        await reg.acquire("session-1")

        # Second acquire should timeout (short timeout for test speed)
        result = await reg.acquire("session-2", timeout=0.1)
        assert result is False

        await reg.release("session-1")

    async def test_acquire_duplicate_raises(self):
        reg = SessionRegistry(max_concurrent=2)
        await reg.acquire("session-1")
        with pytest.raises(RuntimeError, match="already acquired"):
            await reg.acquire("session-1")
        await reg.release("session-1")

    async def test_acquire_multiple_sessions(self):
        reg = SessionRegistry(max_concurrent=3)
        for i in range(3):
            assert await reg.acquire(f"session-{i}")

        # 4th should fail with short timeout
        result = await reg.acquire("session-3", timeout=0.1)
        assert result is False

        for i in range(3):
            await reg.release(f"session-{i}")


class TestRelease:
    async def test_release_without_acquire(self):
        reg = SessionRegistry()
        # Should not raise
        await reg.release("nonexistent")

    async def test_release_idempotent(self):
        reg = SessionRegistry(max_concurrent=1)
        await reg.acquire("session-1")
        await reg.release("session-1")
        # Second release should not raise or corrupt state
        await reg.release("session-1")
        # Should still be able to acquire new session
        assert await reg.acquire("session-2")


class TestKillSession:
    async def test_kill_releases_semaphore(self):
        """P0-NEW-1: kill_session must release the semaphore slot."""
        reg = SessionRegistry(max_concurrent=2)

        # Fill both slots with tasks that block forever
        async def block():
            await asyncio.sleep(3600)

        await reg.acquire("s1")
        await reg.acquire("s2")

        # Create real tasks for both sessions
        task1 = asyncio.create_task(block())
        task2 = asyncio.create_task(block())
        async with reg._lock:
            reg._active_sessions["s1"] = task1
            reg._active_sessions["s2"] = task2

        # All slots occupied — third acquire would timeout
        result = await reg.acquire("s3", timeout=0.1)
        assert result is False

        # Kill s1 — should free a slot
        await reg.kill_session("s1")
        result = await reg.acquire("s3", timeout=1.0)
        assert result is True

        # Cleanup
        task2.cancel()
        try:
            await task2
        except asyncio.CancelledError:
            pass
        await reg.release("s2")
        await reg.release("s3")

    async def test_kill_nonexistent(self):
        reg = SessionRegistry()
        result = await reg.kill_session("does-not-exist")
        assert result is False

    async def test_kill_done_task(self):
        reg = SessionRegistry(max_concurrent=2)
        await reg.acquire("s1")

        async def done_soon():
            return

        task = asyncio.create_task(done_soon())
        await task  # wait for completion

        async with reg._lock:
            reg._active_sessions["s1"] = task

        result = await reg.kill_session("s1")
        # task is done, so no cancel happens; but slot was released
        assert result is False


class TestListActive:
    async def test_list_active(self):
        reg = SessionRegistry(max_concurrent=5)
        assert await reg.list_active() == []

        await reg.acquire("s1")
        await reg.acquire("s2")

        active = await reg.list_active()
        assert set(active) == {"s1", "s2"}

        await reg.release("s1")
        active = await reg.list_active()
        assert active == ["s2"]

        await reg.release("s2")


class TestNoSlotLeak:
    async def test_acquire_release_cycle_many_times(self):
        """Stress test: many acquire/release cycles should not leak slots."""
        reg = SessionRegistry(max_concurrent=2)

        for i in range(20):
            assert await reg.acquire(f"session-{i}")
            await reg.release(f"session-{i}")

        # Should still have 2 slots available
        assert await reg.acquire("final-1")
        assert await reg.acquire("final-2")
        result = await reg.acquire("final-3", timeout=0.1)
        assert result is False
        await reg.release("final-1")
        await reg.release("final-2")
