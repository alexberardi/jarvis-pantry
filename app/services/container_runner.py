"""Strategy pattern for where container tests actually execute.

`LocalRunner` runs Docker on the current machine (dev). `GitHubActionsRunner`
(step 6) dispatches a workflow and returns pending — the finalize step runs
later via the `/v1/submissions/{id}/container-result` callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import get_settings
from .container_test import ContainerTestResult, run_container_tests


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


def get_runner() -> ContainerTestRunner:
    backend = get_settings().container_runner
    if backend == "local":
        return LocalRunner()
    raise ValueError(
        f"Unknown PANTRY_CONTAINER_RUNNER={backend!r}. Expected one of: local",
    )
