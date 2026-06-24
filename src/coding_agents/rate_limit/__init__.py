"""Rate-limit middleware for coding-agents.

Provides three layers of rate limiting:

* :class:`ConcurrencyGate` — semaphore-based concurrency limiting.
* :class:`RateLimitSignalDetector` — 429 signal detection from subprocess output.
* :class:`AdaptiveRateLimiter` — token-bucket rate limiter that adapts to 429 feedback.
* :class:`RateLimitMetrics` — aggregated metrics from all layers.
"""

from __future__ import annotations

from coding_agents.rate_limit.adaptive import AdaptiveRateLimiter
from coding_agents.rate_limit.concurrency import ConcurrencyGate
from coding_agents.rate_limit.detector import RateLimitSignalDetector
from coding_agents.rate_limit.metrics import RateLimitMetrics

__all__ = [
    "AdaptiveRateLimiter",
    "ConcurrencyGate",
    "RateLimitMetrics",
    "RateLimitSignalDetector",
]
