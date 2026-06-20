"""Tests for retry mechanism."""

from __future__ import annotations

import asyncio

import pytest

from coding_agents.retry import RetryError, RetryPolicy, with_retry, with_retry_generator


class TestRetryPolicy:
    """Test RetryPolicy dataclass."""

    def test_should_retry_matching_exception(self):
        """Should retry when exception matches retry_on."""
        policy = RetryPolicy(retry_on=(ValueError, TypeError))
        assert policy.should_retry(ValueError("test")) is True
        assert policy.should_retry(TypeError("test")) is True

    def test_should_retry_non_matching_exception(self):
        """Should not retry when exception doesn't match retry_on."""
        policy = RetryPolicy(retry_on=(ValueError,))
        assert policy.should_retry(TypeError("test")) is False

    def test_should_retry_base_exception(self):
        """Should retry on base Exception by default."""
        policy = RetryPolicy()
        assert policy.should_retry(ValueError("test")) is True
        assert policy.should_retry(RuntimeError("test")) is True

    def test_get_delay_exponential_backoff(self):
        """Delay should increase exponentially."""
        policy = RetryPolicy(delay_seconds=1.0, backoff_multiplier=2.0)
        assert policy.get_delay(0) == 1.0  # 1 * 2^0
        assert policy.get_delay(1) == 2.0  # 1 * 2^1
        assert policy.get_delay(2) == 4.0  # 1 * 2^2
        assert policy.get_delay(3) == 8.0  # 1 * 2^3

    def test_get_delay_custom_multiplier(self):
        """Delay should respect custom backoff multiplier."""
        policy = RetryPolicy(delay_seconds=0.5, backoff_multiplier=3.0)
        assert policy.get_delay(0) == 0.5
        assert policy.get_delay(1) == 1.5
        assert policy.get_delay(2) == 4.5


class TestWithRetry:
    """Test with_retry async function."""

    async def test_success_no_retry(self):
        """Should succeed on first attempt without retry."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await with_retry(lambda: operation(), RetryPolicy(max_retries=3))
        assert result == "success"
        assert call_count == 1

    async def test_retry_on_failure(self):
        """Should retry on failure and eventually succeed."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError(f"attempt {call_count}")
            return "success"

        policy = RetryPolicy(max_retries=3, delay_seconds=0.01, retry_on=(ValueError,))
        result = await with_retry(lambda: operation(), policy)
        assert result == "success"
        assert call_count == 3

    async def test_retry_exhausted(self):
        """Should raise RetryError when all retries exhausted."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"always fails: {call_count}")

        policy = RetryPolicy(max_retries=2, delay_seconds=0.01, retry_on=(ValueError,))
        with pytest.raises(RetryError) as exc_info:
            await with_retry(lambda: operation(), policy)

        assert call_count == 3  # 1 initial + 2 retries
        assert exc_info.value.last_exception is not None
        assert "always fails: 3" in str(exc_info.value.last_exception)

    async def test_no_retry_on_non_matching_exception(self):
        """Should not retry when exception doesn't match retry_on."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise TypeError("wrong type")

        policy = RetryPolicy(max_retries=3, delay_seconds=0.01, retry_on=(ValueError,))
        with pytest.raises(TypeError, match="wrong type"):
            await with_retry(lambda: operation(), policy)

        assert call_count == 1  # No retry

    async def test_zero_retries(self):
        """Should not retry when max_retries=0."""
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            raise ValueError("fail")

        policy = RetryPolicy(max_retries=0, delay_seconds=0.01)
        with pytest.raises(RetryError):
            await with_retry(lambda: operation(), policy)

        assert call_count == 1


class TestWithRetryGenerator:
    """Test with_retry_generator for async generators."""

    async def test_generator_success_no_retry(self):
        """Should succeed on first attempt without retry."""
        call_count = 0

        async def gen():
            nonlocal call_count
            call_count += 1
            yield 1
            yield 2
            yield 3

        policy = RetryPolicy(max_retries=3)
        results = []
        async for item in with_retry_generator(lambda: gen(), policy):
            results.append(item)

        assert results == [1, 2, 3]
        assert call_count == 1

    async def test_generator_retry_on_failure(self):
        """Should retry generator on failure."""
        call_count = 0

        async def gen():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ValueError(f"attempt {call_count}")
            yield 1
            yield 2

        policy = RetryPolicy(max_retries=3, delay_seconds=0.01, retry_on=(ValueError,))
        results = []
        async for item in with_retry_generator(lambda: gen(), policy):
            results.append(item)

        assert results == [1, 2]
        assert call_count == 2

    async def test_generator_retry_exhausted(self):
        """Should raise RetryError when generator retries exhausted."""
        call_count = 0

        async def gen():
            nonlocal call_count
            call_count += 1
            yield 1  # Yield first to make it a generator
            raise ValueError(f"always fails: {call_count}")

        policy = RetryPolicy(max_retries=2, delay_seconds=0.01, retry_on=(ValueError,))
        results = []
        with pytest.raises(RetryError) as exc_info:
            async for item in with_retry_generator(lambda: gen(), policy):
                results.append(item)

        # Should have tried 3 times (1 initial + 2 retries), each yielding 1 item
        assert call_count == 3
        assert len(results) == 3  # One item from each attempt
        assert exc_info.value.last_exception is not None

    async def test_generator_partial_failure(self):
        """Should retry when generator fails mid-stream."""
        call_count = 0

        async def gen():
            nonlocal call_count
            call_count += 1
            yield 1
            if call_count < 2:
                raise ValueError("mid-stream failure")
            yield 2
            yield 3

        policy = RetryPolicy(max_retries=3, delay_seconds=0.01, retry_on=(ValueError,))
        results = []
        async for item in with_retry_generator(lambda: gen(), policy):
            results.append(item)

        # First attempt yields 1 then fails, second attempt yields all
        assert results == [1, 1, 2, 3]
        assert call_count == 2
