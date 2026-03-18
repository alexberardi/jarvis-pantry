"""Tests for the rate limiter."""

from app.rate_limiter import RateBucket, RateLimiter


class TestRateBucket:
    def test_allows_under_limit(self):
        bucket = RateBucket(window_seconds=3600, max_requests=5)
        for _ in range(5):
            assert bucket.allow() is True

    def test_rejects_over_limit(self):
        bucket = RateBucket(window_seconds=3600, max_requests=2)
        assert bucket.allow() is True
        assert bucket.allow() is True
        assert bucket.allow() is False

    def test_remaining(self):
        bucket = RateBucket(window_seconds=3600, max_requests=5)
        assert bucket.remaining == 5
        bucket.allow()
        assert bucket.remaining == 4


class TestRateLimiter:
    def test_different_keys_independent(self):
        limiter = RateLimiter(requests_per_hour=2)
        assert limiter.check("a") is True
        assert limiter.check("a") is True
        assert limiter.check("a") is False  # a exhausted
        assert limiter.check("b") is True   # b still has capacity

    def test_remaining(self):
        limiter = RateLimiter(requests_per_hour=10)
        limiter.check("x")
        assert limiter.remaining("x") == 9
