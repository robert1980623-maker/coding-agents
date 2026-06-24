"""Tests for RateLimitSignalDetector."""

from __future__ import annotations

import pytest

from coding_agents.rate_limit.detector import RateLimitSignalDetector


class TestRateLimitSignalDetector:
    """Test RateLimitSignalDetector pattern matching."""

    def test_no_signal_by_default(self):
        """Fresh detector should not report any signal."""
        d = RateLimitSignalDetector()
        assert d.detected is False
        assert d.signals == []

    def test_detects_429(self):
        """Should match plain '429'."""
        d = RateLimitSignalDetector()
        assert d.check("HTTP 429") is True
        assert d.detected is True
        assert "HTTP 429" in d.signals

    def test_detects_rate_limit(self):
        """Should match 'rate limit' (with space)."""
        d = RateLimitSignalDetector()
        assert d.check("hit the rate limit") is True
        assert d.detected is True

    def test_detects_ratelimit_no_space(self):
        """Should match 'ratelimit' (no space)."""
        d = RateLimitSignalDetector()
        assert d.check("ratelimit exceeded") is True

    def test_detects_rate_limit_with_hyphen(self):
        """Should match 'rate-limit' (with hyphen, since .? matches one char)."""
        d = RateLimitSignalDetector()
        assert d.check("rate-limit error") is True

    def test_detects_too_many_requests(self):
        """Should match 'too many requests'."""
        d = RateLimitSignalDetector()
        assert d.check("Too Many Requests") is True

    def test_detects_quota_exceeded(self):
        """Should match 'quota exceeded'."""
        d = RateLimitSignalDetector()
        assert d.check("quota exceeded") is True

    def test_detects_quotaexceeded_no_space(self):
        """Should match 'quotaexceeded' (no space)."""
        d = RateLimitSignalDetector()
        assert d.check("quotaExceeded") is True

    def test_detects_concurrency_limit(self):
        """Should match 'concurrency limit'."""
        d = RateLimitSignalDetector()
        assert d.check("concurrency limit reached") is True

    def test_detects_concurrencylimit_no_space(self):
        """Should match 'concurrencylimit' (no space)."""
        d = RateLimitSignalDetector()
        assert d.check("concurrencylimit") is True

    def test_detects_throttled(self):
        """Should match 'throttled'."""
        d = RateLimitSignalDetector()
        assert d.check("request throttled") is True

    def test_detects_throttling(self):
        """Should match 'throttling'."""
        d = RateLimitSignalDetector()
        assert d.check("throttling active") is True

    def test_case_insensitive(self):
        """Patterns should be case-insensitive."""
        d = RateLimitSignalDetector()
        assert d.check("RATE LIMIT") is True
        d2 = RateLimitSignalDetector()
        assert d2.check("Rate_Limit") is True
        d3 = RateLimitSignalDetector()
        assert d3.check("TOO MANY REQUESTS") is True

    def test_no_match(self):
        """Should return False and not set detected for normal lines."""
        d = RateLimitSignalDetector()
        assert d.check("normal output line") is False
        assert d.detected is False
        assert d.signals == []

    def test_signals_last_10(self):
        """Should keep only the last 10 signals."""
        d = RateLimitSignalDetector()
        for i in range(15):
            d.check(f"429 error {i}")
        assert len(d.signals) == 10
        # Oldest retained should be #5 (0..4 were evicted).
        assert d.signals[0] == "429 error 5"
        assert d.signals[-1] == "429 error 14"

    def test_reset(self):
        """reset() should clear all state."""
        d = RateLimitSignalDetector()
        d.check("429")
        assert d.detected is True
        assert len(d.signals) == 1

        d.reset()
        assert d.detected is False
        assert d.signals == []

    def test_detected_persists_after_no_match(self):
        """detected should stay True even after subsequent non-matching lines."""
        d = RateLimitSignalDetector()
        d.check("429 error")
        d.check("normal line")
        d.check("another normal line")
        assert d.detected is True
        assert len(d.signals) == 1

    def test_multiple_signals_recorded(self):
        """Multiple matching lines should all be recorded."""
        d = RateLimitSignalDetector()
        d.check("429 error")
        d.check("throttled")
        d.check("rate limit hit")
        assert len(d.signals) == 3
        assert "429 error" in d.signals
        assert "throttled" in d.signals
        assert "rate limit hit" in d.signals

    def test_429_not_matched_in_larger_number(self):
        """'429' inside a larger number (e.g. 4290) must not match."""
        d = RateLimitSignalDetector()
        assert d.check("processed 4290 items") is False
        assert d.detected is False
        assert d.check("request 14291 failed") is False
        assert d.detected is False

    def test_429_still_matches_standalone(self):
        """Bare '429' and 'HTTP 429' must still match after the regex fix."""
        d = RateLimitSignalDetector()
        assert d.check("429") is True
        d2 = RateLimitSignalDetector()
        assert d2.check("HTTP 429 Too Many Requests") is True
        d3 = RateLimitSignalDetector()
        assert d3.check("status=429") is True
