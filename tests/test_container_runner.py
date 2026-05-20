"""Tests for the container-test runner strategies (#21).

Covers dispatch-payload shape: the GitHubActionsRunner now sends a
``lockfile_content`` workflow input instead of the legacy ``packages`` list.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.container_runner import (
    ContainerTestRunner,
    GitHubActionsRunner,
    LocalRunner,
)


class _FakeResponse:
    def __init__(self, status_code: int = 204, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class TestGitHubActionsDispatchPayload:
    """workflow_dispatch payload shape — ``lockfile_content`` in, ``packages`` out."""

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_payload_contains_lockfile_content_input(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        settings = mock_settings.return_value
        settings.pantry_gh_token = "ghp_x"
        settings.pantry_callback_base_url = "https://pantry.example/"
        settings.pantry_runner_repo = "alexberardi/jarvis-pantry-runner"
        settings.pantry_runner_workflow = "container-test.yml"
        settings.pantry_runner_ref = "main"

        post_mock = AsyncMock(return_value=_FakeResponse(204))
        client_instance = MagicMock()
        client_instance.post = post_mock
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = client_instance

        await GitHubActionsRunner().dispatch(
            command_dir=tmp_path,
            submission_id=42,
            lockfile_content="requests==2.31.0 --hash=sha256:abc\n",
            is_bundle=False,
            repo_url="https://github.com/test/repo",
        )

        assert post_mock.call_count == 1
        _args, kwargs = post_mock.call_args
        payload = kwargs.get("json") or (_args[1] if len(_args) > 1 else {})
        inputs = payload["inputs"]
        assert inputs["lockfile_content"] == "requests==2.31.0 --hash=sha256:abc\n"
        # Regression guard — legacy key must be gone from the wire shape
        assert "packages" not in inputs

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_payload_drops_packages_input_entirely(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        """Defense-in-depth: ``packages`` is absent even on empty lockfile content."""
        settings = mock_settings.return_value
        settings.pantry_gh_token = "ghp_x"
        settings.pantry_callback_base_url = "https://pantry.example/"
        settings.pantry_runner_repo = "alexberardi/jarvis-pantry-runner"
        settings.pantry_runner_workflow = "container-test.yml"
        settings.pantry_runner_ref = "main"

        post_mock = AsyncMock(return_value=_FakeResponse(204))
        client_instance = MagicMock()
        client_instance.post = post_mock
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = client_instance

        await GitHubActionsRunner().dispatch(
            command_dir=tmp_path,
            submission_id=7,
            lockfile_content="",
            is_bundle=False,
            repo_url="https://github.com/test/repo",
        )
        _args, kwargs = post_mock.call_args
        inputs = kwargs["json"]["inputs"]
        assert inputs["lockfile_content"] == ""
        assert "packages" not in inputs

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_dispatch_propagates_runtime_errors(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        """5xx from GitHub still raises RuntimeError — refactor must not swallow."""
        settings = mock_settings.return_value
        settings.pantry_gh_token = "ghp_x"
        settings.pantry_callback_base_url = "https://pantry.example/"
        settings.pantry_callback_signing_key = "k" * 32
        settings.pantry_runner_repo = "alexberardi/jarvis-pantry-runner"
        settings.pantry_runner_workflow = "container-test.yml"
        settings.pantry_runner_ref = "main"

        post_mock = AsyncMock(return_value=_FakeResponse(500, text="bad"))
        client_instance = MagicMock()
        client_instance.post = post_mock
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = client_instance

        with pytest.raises(RuntimeError, match="failed"):
            await GitHubActionsRunner().dispatch(
                command_dir=tmp_path,
                submission_id=8,
                lockfile_content="",
                is_bundle=False,
                repo_url="https://github.com/test/repo",
            )


class TestCallbackHmacDispatchShape:
    """Wire-shape contract for the HMAC callback (#25).

    The new contract: `nonce` is a workflow input (public); `callback_token`
    is gone. The signing key never crosses the workflow_dispatch boundary —
    it lives only in the server's settings and the runner's GHA-env secret.
    """

    def _settings(self, mock_settings):
        settings = mock_settings.return_value
        settings.pantry_gh_token = "ghp_x"
        settings.pantry_callback_base_url = "https://pantry.example/"
        settings.pantry_callback_signing_key = "server-side-secret-32-bytes-of-stuff!!"
        settings.pantry_runner_repo = "alexberardi/jarvis-pantry-runner"
        settings.pantry_runner_workflow = "container-test.yml"
        settings.pantry_runner_ref = "main"
        return settings

    def _patched_client(self, mock_client_cls):
        post_mock = AsyncMock(return_value=_FakeResponse(204))
        client_instance = MagicMock()
        client_instance.post = post_mock
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = client_instance
        return post_mock

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_payload_contains_nonce_and_no_callback_token(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        self._settings(mock_settings)
        post_mock = self._patched_client(mock_client_cls)

        await GitHubActionsRunner().dispatch(
            command_dir=tmp_path,
            submission_id=11,
            lockfile_content="",
            is_bundle=False,
            repo_url="https://github.com/test/repo",
        )

        _args, kwargs = post_mock.call_args
        inputs = kwargs["json"]["inputs"]
        assert "nonce" in inputs
        assert isinstance(inputs["nonce"], str) and len(inputs["nonce"]) >= 32
        # Hard cut: the signing key must never leak through workflow_dispatch,
        # and the legacy one-time token must be gone from the wire.
        assert "callback_token" not in inputs
        assert "signing_key" not in inputs
        assert "pantry_callback_signing_key" not in inputs

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_dispatch_returns_nonce_for_persistence(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        self._settings(mock_settings)
        post_mock = self._patched_client(mock_client_cls)

        dispatch = await GitHubActionsRunner().dispatch(
            command_dir=tmp_path,
            submission_id=12,
            lockfile_content="",
            is_bundle=False,
            repo_url="https://github.com/test/repo",
        )
        _args, kwargs = post_mock.call_args
        inputs = kwargs["json"]["inputs"]
        assert dispatch.callback_nonce == inputs["nonce"]
        # Legacy attribute is gone from the dataclass.
        assert not hasattr(dispatch, "callback_token")

    @pytest.mark.asyncio
    @patch("app.services.container_runner.get_settings")
    @patch("app.services.container_runner.httpx.AsyncClient")
    async def test_fails_fast_when_signing_key_unset(
        self, mock_client_cls, mock_settings, tmp_path,
    ):
        settings = self._settings(mock_settings)
        settings.pantry_callback_signing_key = ""
        self._patched_client(mock_client_cls)

        with pytest.raises(RuntimeError, match="PANTRY_CALLBACK_SIGNING_KEY"):
            await GitHubActionsRunner().dispatch(
                command_dir=tmp_path,
                submission_id=13,
                lockfile_content="",
                is_bundle=False,
                repo_url="https://github.com/test/repo",
            )


class TestRunnerSignatures:
    """Protocol + both implementations match the new contract (#21)."""

    def test_protocol_signature_drops_packages_and_accepts_lockfile_content(self):
        sig = inspect.signature(ContainerTestRunner.dispatch)
        assert "lockfile_content" in sig.parameters
        assert "packages" not in sig.parameters

    def test_local_runner_signature_drops_packages_and_accepts_lockfile_content(self):
        sig = inspect.signature(LocalRunner.dispatch)
        assert "lockfile_content" in sig.parameters
        assert "packages" not in sig.parameters

    def test_github_runner_signature_drops_packages_and_accepts_lockfile_content(self):
        sig = inspect.signature(GitHubActionsRunner.dispatch)
        assert "lockfile_content" in sig.parameters
        assert "packages" not in sig.parameters

    @pytest.mark.asyncio
    @patch("app.services.container_runner.run_container_tests", new_callable=AsyncMock)
    async def test_local_runner_accepts_lockfile_content_kwarg(
        self, mock_run_tests, tmp_path,
    ):
        from app.services.container_test import ContainerTestResult
        mock_run_tests.return_value = ContainerTestResult(
            passed=True, summary="ok", test_count=1, pass_count=1, fail_count=0,
        )
        dispatch = await LocalRunner().dispatch(
            command_dir=tmp_path,
            submission_id=1,
            lockfile_content="",
            is_bundle=False,
            repo_url="https://github.com/test/repo",
        )
        # Call compiles + runs; the dispatch result has the runner's result populated
        assert dispatch.result is not None
        assert dispatch.result.passed is True
