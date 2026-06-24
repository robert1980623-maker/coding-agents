"""Rate-limit signal detection from subprocess output.

Scans lines of text (typically stderr/stdout from a child process) for
patterns that indicate an upstream service is rate-limiting the caller.
"""

from __future__ import annotations

import re
from collections import deque

import structlog

logger = structlog.get_logger(__name__)


class RateLimitSignalDetector:
    """Detects 429 signals from subprocess output.

    Each call to :meth:`check` tests a single line of output against a set
    of compiled regex patterns. When a match is found the line is recorded
    (up to a rolling window of 10) and the :attr:`detected` flag is set.

    The detector is stateful: once :attr:`detected` becomes ``True`` it
    stays ``True`` for the lifetime of the instance, even if subsequent
    lines no longer match. Call :meth:`reset` to clear state.
    """

    PATTERNS: list[re.Pattern[str]] = [
        # Use word boundaries so e.g. "processed 4290 items" does not
        # get flagged as a 429 response.
        re.compile(r"\b429\b", re.IGNORECASE),
        re.compile(r"rate.?limit", re.IGNORECASE),
        re.compile(r"too many requests", re.IGNORECASE),
        re.compile(r"quota.?exceeded", re.IGNORECASE),
        re.compile(r"concurrency.?limit", re.IGNORECASE),
        re.compile(r"throttl", re.IGNORECASE),
    ]

    def __init__(self) -> None:
        self._detected: bool = False
        self._signals: deque[str] = deque(maxlen=10)

    def check(self, line: str) -> bool:
        """Check a single line of output for rate-limit signals.

        Args:
            line: A single line of text, typically from subprocess stdout
                or stderr.

        Returns:
            ``True`` if the line matched any rate-limit pattern, ``False``
            otherwise.
        """
        for pattern in self.PATTERNS:
            if pattern.search(line):
                self._detected = True
                self._signals.append(line)
                logger.info(
                    "rate_limit_signal_detected",
                    pattern=pattern.pattern,
                    line=line[:200],
                )
                return True
        return False

    @property
    def detected(self) -> bool:
        """Whether any rate-limit signal has been observed."""
        return self._detected

    @property
    def signals(self) -> list[str]:
        """Return the last 10 matching lines (oldest first)."""
        return list(self._signals)

    def reset(self) -> None:
        """Clear all state, including the detected flag and signal history."""
        self._detected = False
        self._signals.clear()
        logger.debug("rate_limit_detector_reset")
