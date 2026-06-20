"""Pytest fixtures for benchmark tests."""

from __future__ import annotations

import pytest

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


@pytest.fixture
def psutil_available():
    """Check if psutil is available for memory measurement."""
    if not HAS_PSUTIL:
        pytest.skip("psutil not installed")
    return True


@pytest.fixture
def process_monitor():
    """Monitor current process memory and CPU."""
    if not HAS_PSUTIL:
        pytest.skip("psutil not installed")

    proc = psutil.Process()
    memory_samples = []
    cpu_samples = []

    class Monitor:
        def sample(self):
            """Take a sample of memory and CPU usage."""
            memory_samples.append(proc.memory_info().rss / 1024 / 1024)  # MB
            cpu_samples.append(proc.cpu_percent(interval=None))

        @property
        def peak_memory_mb(self):
            """Peak memory in MB."""
            return max(memory_samples) if memory_samples else 0.0

        @property
        def avg_cpu_percent(self):
            """Average CPU usage."""
            return sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0

    return Monitor()
