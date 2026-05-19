"""Tests for the rate limiter."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


class TestLiveCap:
    """Tests for the live-cap (cap_source) code path added by #31."""

    # ── Backward compatibility (static cap path must not regress) ──

    def test_static_requests_per_hour_still_works(self):
        limiter = RateLimiter(requests_per_hour=3)
        assert limiter.check("ip") is True
        assert limiter.check("ip") is True
        assert limiter.check("ip") is True
        assert limiter.check("ip") is False

    def test_default_constructor_signature_unchanged(self):
        limiter = RateLimiter()
        assert limiter._cap_source is None
        assert limiter.requests_per_hour == 100

    def test_remaining_static_path_unchanged(self):
        limiter = RateLimiter(requests_per_hour=10)
        limiter.check("x")
        assert limiter.remaining("x") == 9

    # ── cap_source callable wiring ──

    def test_cap_source_consulted_on_check(self):
        mock_source = MagicMock(return_value=5)
        limiter = RateLimiter(cap_source=mock_source)
        limiter.check("a")
        assert mock_source.called is True

    def test_cap_source_overrides_requests_per_hour_arg(self):
        limiter = RateLimiter(requests_per_hour=2, cap_source=lambda: 10)
        for _ in range(5):
            assert limiter.check("ip") is True

    # ── Live-cap re-read (the core fix) ──

    def test_cap_increase_takes_effect_mid_session(self):
        holder = {"cap": 3}
        limiter = RateLimiter(cap_source=lambda: holder["cap"])
        for _ in range(3):
            assert limiter.check("ip") is True
        assert limiter.check("ip") is False  # at cap=3
        holder["cap"] = 10
        assert limiter.check("ip") is True   # post-bump, accepted

    def test_cap_decrease_takes_effect_mid_session(self):
        holder = {"cap": 10}
        limiter = RateLimiter(cap_source=lambda: holder["cap"])
        for _ in range(5):
            assert limiter.check("ip") is True
        holder["cap"] = 4
        assert limiter.check("ip") is False  # 5 timestamps >= new cap=4

    def test_cap_source_invoked_per_check_not_per_construct(self):
        values = iter([2, 2, 100])
        limiter = RateLimiter(cap_source=lambda: next(values))
        assert limiter.check("ip") is True   # cap=2, count=1
        assert limiter.check("ip") is True   # cap=2, count=2
        assert limiter.check("ip") is True   # cap=100, count=3 — accepted because cap re-read

    def test_remaining_reflects_live_cap(self):
        holder = {"cap": 5}
        limiter = RateLimiter(cap_source=lambda: holder["cap"])
        limiter.check("x")
        assert limiter.remaining("x") == 4
        holder["cap"] = 20
        assert limiter.remaining("x") == 19

    # ── Helper and wiring ──

    def test_submission_cap_helper_returns_current_settings_value(self, monkeypatch):
        from app import rate_limiter as rl_mod
        import app.config as config_mod

        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: SimpleNamespace(submission_rate_limit_per_hour=7),
        )
        assert rl_mod._submission_cap_per_hour() == 7

        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: SimpleNamespace(submission_rate_limit_per_hour=42),
        )
        assert rl_mod._submission_cap_per_hour() == 42

    def test_submit_limiter_wired_with_cap_source(self):
        from app.api import submit as submit_mod
        assert submit_mod._submit_limiter._cap_source is not None

    # ── _is_disabled() short-circuit must still fire first ──

    def test_disabled_short_circuit_bypasses_cap_source(self, monkeypatch):
        from app import rate_limiter as rl_mod
        monkeypatch.setattr(rl_mod, "_is_disabled", lambda: True)
        limiter = RateLimiter(cap_source=lambda: 0)
        assert limiter.check("ip") is True

    def test_disabled_short_circuit_in_remaining_returns_999(self, monkeypatch):
        from app import rate_limiter as rl_mod
        monkeypatch.setattr(rl_mod, "_is_disabled", lambda: True)
        limiter = RateLimiter(cap_source=lambda: 0)
        assert limiter.remaining("ip") == 999

    # ── Edge cases — cap_source pathological returns ──

    def test_cap_source_returning_zero_rejects_all(self):
        limiter = RateLimiter(cap_source=lambda: 0)
        assert limiter.check("ip") is False

    def test_different_keys_independent_under_cap_source(self):
        limiter = RateLimiter(cap_source=lambda: 2)
        assert limiter.check("a") is True
        assert limiter.check("a") is True
        assert limiter.check("a") is False
        assert limiter.check("b") is True

    # ── Error path ──

    def test_cap_source_raising_propagates(self):
        def boom():
            raise RuntimeError("config blew up")

        limiter = RateLimiter(cap_source=boom)
        with pytest.raises(RuntimeError, match="config blew up"):
            limiter.check("ip")
