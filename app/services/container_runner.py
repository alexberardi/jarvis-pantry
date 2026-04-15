"""Strategy pattern for where container tests actually execute.

`LocalRunner` runs Docker on the current machine (dev). `GitHubActionsRunner`
dispatches a workflow and returns pending — the finalize step runs later
via the `/v1/submissions/{id}/container-result` callback.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from ..config import get_settings
from .container_test import ContainerTestResult, run_container_tests

logger = logging.getLogger(__name__)


@dataclass
class RunnerDispatch:
    """Outcome of dispatching a container test.

    - Synchronous runners return `result` populated and `pending` False.
    - Async runners return `pending` True with an `external_run_url` and a
      `callback_token` the remote runner will include in its callback.
    """

    result: ContainerTestResult | None
    external_run_url: str | None = None
    callback_token: str | None = None

    @property
    def pending(self) -> bool:
        return self.result is None


class ContainerTestRunner(Protocol):
    async def dispatch(
        self,
        *,
        command_dir: Path,
        submission_id: int,
        packages: list[str] | None,
        is_bundle: bool,
        repo_url: str,
    ) -> RunnerDispatch: ...


class LocalRunner:
    """Runs tests in Docker on the current host."""

    async def dispatch(
        self,
        *,
        command_dir: Path,
        submission_id: int,
        packages: list[str] | None,
        is_bundle: bool,
        repo_url: str,
    ) -> RunnerDispatch:
        result = await run_container_tests(
            command_dir=command_dir,
            submission_id=submission_id,
            packages=packages,
            is_bundle=is_bundle,
        )
        return RunnerDispatch(result=result)


class GitHubActionsRunner:
    """Dispatches the container-test workflow in a public runner repo.

    The workflow runs the SDK harness against the submitted repo on an
    isolated Actions VM, then POSTs the result to
    `{base_url}/v1/submissions/{id}/container-result` with the per-dispatch
    `X-Pantry-Token` header.

    `workflow_dispatch` doesn't return a run id, so `external_run_url`
    points at the workflow's run listing — users can find their submission
    by submission_id in the run title.
    """

    async def dispatch(
        self,
        *,
        command_dir: Path,
        submission_id: int,
        packages: list[str] | None,
        is_bundle: bool,
        repo_url: str,
    ) -> RunnerDispatch:
        settings = get_settings()
        if not settings.pantry_gh_token:
            raise RuntimeError("PANTRY_GH_TOKEN is not set — cannot dispatch GitHub Actions runner")
        if not settings.pantry_callback_base_url:
            raise RuntimeError("PANTRY_CALLBACK_BASE_URL is not set — runner would have nowhere to call back")

        owner_repo = settings.pantry_runner_repo
        workflow = settings.pantry_runner_workflow
        ref = settings.pantry_runner_ref
        token = secrets.token_urlsafe(32)
        callback_url = (
            f"{settings.pantry_callback_base_url.rstrip('/')}"
            f"/v1/submissions/{submission_id}/container-result"
        )

        url = f"https://api.github.com/repos/{owner_repo}/actions/workflows/{workflow}/dispatches"
        payload = {
            "ref": ref,
            "inputs": {
                "repo_url": repo_url,
                "submission_id": str(submission_id),
                "callback_url": callback_url,
                "callback_token": token,
                "is_bundle": "true" if is_bundle else "false",
                "packages": json.dumps(packages or []),
            },
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.pantry_gh_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=payload,
            )
        if resp.status_code not in (201, 204):
            raise RuntimeError(
                f"GitHub workflow_dispatch failed: {resp.status_code} {resp.text[:500]}",
            )
        logger.info(
            "Dispatched submission %d to %s/%s (ref=%s)",
            submission_id, owner_repo, workflow, ref,
        )

        run_url = f"https://github.com/{owner_repo}/actions/workflows/{workflow}"
        return RunnerDispatch(result=None, external_run_url=run_url, callback_token=token)


def get_runner() -> ContainerTestRunner:
    backend = get_settings().container_runner
    if backend == "local":
        return LocalRunner()
    if backend == "github_actions":
        return GitHubActionsRunner()
    raise ValueError(
        f"Unknown PANTRY_CONTAINER_RUNNER={backend!r}. "
        "Expected one of: local, github_actions",
    )
