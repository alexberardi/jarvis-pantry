"""Startup fail-fast checks on PANTRY_CALLBACK_SIGNING_KEY (#25).

When the dispatch backend is `github_actions`, the server has to verify HMAC
on every callback — so it has to have the signing key at startup. Booting
without it would silently 500 every async callback once the queue starts
dispatching, which is worse than failing fast.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app, lifespan


def _settings(*, container_runner: str, signing_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        container_runner=container_runner,
        pantry_callback_signing_key=signing_key,
        max_concurrent_container_tests=1,
    )


class TestStartupFailFast:
    @pytest.mark.asyncio
    async def test_github_actions_without_signing_key_fails_fast(self):
        with patch("app.main.get_settings", return_value=_settings(
            container_runner="github_actions", signing_key="",
        )), patch("app.main.validation_queue.start", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="PANTRY_CALLBACK_SIGNING_KEY"):
                async with lifespan(app):
                    pass

    @pytest.mark.asyncio
    async def test_github_actions_with_signing_key_starts(self):
        with patch("app.main.get_settings", return_value=_settings(
            container_runner="github_actions", signing_key="k" * 32,
        )), patch("app.main.validation_queue.start", new_callable=AsyncMock), \
           patch("app.main.validation_queue.stop", new_callable=AsyncMock), \
           patch("app.main._cleanup_stale_repos"), \
           patch("app.main.callback_timeout_watcher", new_callable=AsyncMock):
            async with lifespan(app):
                pass  # boots cleanly

    @pytest.mark.asyncio
    async def test_local_runner_without_signing_key_starts(self):
        """Dev / local runner doesn't need a signing key — that path never
        invokes the GHA callback, so a missing key is fine."""
        with patch("app.main.get_settings", return_value=_settings(
            container_runner="local", signing_key="",
        )), patch("app.main.validation_queue.start", new_callable=AsyncMock), \
           patch("app.main.validation_queue.stop", new_callable=AsyncMock), \
           patch("app.main._cleanup_stale_repos"), \
           patch("app.main.callback_timeout_watcher", new_callable=AsyncMock):
            async with lifespan(app):
                pass


class TestSettingsField:
    def test_pantry_callback_signing_key_reads_env(self, monkeypatch):
        from app.config import Settings
        monkeypatch.setenv("PANTRY_CALLBACK_SIGNING_KEY", "sk-test-12345678901234567890")
        s = Settings(_env_file=None)
        assert s.pantry_callback_signing_key == "sk-test-12345678901234567890"

    def test_pantry_callback_signing_key_defaults_empty(self, monkeypatch):
        from app.config import Settings
        monkeypatch.delenv("PANTRY_CALLBACK_SIGNING_KEY", raising=False)
        s = Settings(_env_file=None)
        assert s.pantry_callback_signing_key == ""
