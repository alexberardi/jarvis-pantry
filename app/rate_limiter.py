"""In-memory rate limiter for the command store.

Set RATE_LIMIT_DISABLED=true to bypass all rate checks (dev/testing).
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field


def _is_disabled() -> bool:
    from app.config import get_settings
    return get_settings().rate_limit_disabled


@dataclass
class RateBucket:
    """Sliding-window counter for rate limiting."""

    window_seconds: int
    max_requests: int
    timestamps: list[float] = field(default_factory=list)

    def allow(self) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        if len(self.timestamps) >= self.max_requests:
            return False
        self.timestamps.append(now)
        return True

    @property
    def remaining(self) -> int:
        now = time.time()
        cutoff = now - self.window_seconds
        active = sum(1 for t in self.timestamps if t > cutoff)
        return max(0, self.max_requests - active)


class RateLimiter:
    """Per-IP rate limiter."""

    def __init__(self, requests_per_hour: int = 100):
        self.requests_per_hour = requests_per_hour
        self._buckets: dict[str, RateBucket] = defaultdict(
            lambda: RateBucket(window_seconds=3600, max_requests=self.requests_per_hour)
        )

    def check(self, key: str) -> bool:
        """Check if a request from key is allowed."""
        if _is_disabled():
            return True
        return self._buckets[key].allow()

    def remaining(self, key: str) -> int:
        """Get remaining requests for a key."""
        if _is_disabled():
            return 999
        return self._buckets[key].remaining


# Module singleton
rate_limiter = RateLimiter()
