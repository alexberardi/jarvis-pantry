"""In-memory rate limiter for the command store.

Set RATE_LIMIT_DISABLED=true to bypass all rate checks (dev/testing).
"""

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional


def _is_disabled() -> bool:
    from app.config import get_settings
    return get_settings().rate_limit_disabled


def _submission_cap_per_hour() -> int:
    """Live-read of the per-hour submit cap. Re-evaluated on every check so a
    settings change (after a `get_settings.cache_clear()`, or fresh process start
    with a new env var) takes effect without re-importing this module."""
    from app.config import get_settings
    return get_settings().submission_rate_limit_per_hour


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
    """Per-IP rate limiter.

    Static cap (default): pass ``requests_per_hour=N``. The cap is fixed for
    the lifetime of the limiter instance.

    Live cap: pass ``cap_source=callable`` returning the current cap on each
    call. The cap is re-read on every check, so a settings change is honored
    without rebuilding the limiter.
    """

    def __init__(
        self,
        requests_per_hour: int = 100,
        *,
        cap_source: Optional[Callable[[], int]] = None,
    ):
        self.requests_per_hour = requests_per_hour
        self._cap_source = cap_source
        self._buckets: dict[str, RateBucket] = defaultdict(
            lambda: RateBucket(window_seconds=3600, max_requests=self._current_cap())
        )

    def _current_cap(self) -> int:
        if self._cap_source is not None:
            return self._cap_source()
        return self.requests_per_hour

    def check(self, key: str) -> bool:
        """Check if a request from key is allowed."""
        if _is_disabled():
            return True
        bucket = self._buckets[key]
        # Keep the bucket's cap in sync with the live cap so subsequent
        # allow()/remaining calls reflect the current setting.
        bucket.max_requests = self._current_cap()
        return bucket.allow()

    def remaining(self, key: str) -> int:
        """Get remaining requests for a key."""
        if _is_disabled():
            return 999
        bucket = self._buckets[key]
        bucket.max_requests = self._current_cap()
        return bucket.remaining


# Module singleton
rate_limiter = RateLimiter()
