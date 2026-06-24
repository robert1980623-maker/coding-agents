"""Aggregated rate-limit metrics.

Combines metrics from :class:`ConcurrencyGate` and
:class:`AdaptiveRateLimiter` into a single snapshot suitable for logging
or exporting to a monitoring system.
"""

from __future__ import annotations

from coding_agents.rate_limit.adaptive import AdaptiveRateLimiter
from coding_agents.rate_limit.concurrency import ConcurrencyGate

import structlog

logger = structlog.get_logger(__name__)


class RateLimitMetrics:
    """Aggregates metrics from all rate-limit layers.

    Holds references to a :class:`ConcurrencyGate` and an
    :class:`AdaptiveRateLimiter` and exposes a unified
    :meth:`snapshot` method that returns a nested dictionary of their
    current metrics.
    """

    def __init__(
        self,
        gate: ConcurrencyGate,
        limiter: AdaptiveRateLimiter,
    ) -> None:
        """Initialize the metrics aggregator.

        Args:
            gate: The concurrency gate to read metrics from.
            limiter: The adaptive rate limiter to read metrics from.
        """
        self._gate = gate
        self._limiter = limiter

    def snapshot(self) -> dict:
        """Return a point-in-time snapshot of all rate-limit metrics.

        Returns:
            Dictionary with ``concurrency`` (from the gate) and ``limiter``
            (from the adaptive limiter) sub-dictionaries.
        """
        return {
            "concurrency": self._gate.metrics,
            "limiter": self._limiter.metrics,
        }
