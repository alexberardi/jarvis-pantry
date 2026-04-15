"""Strategy pattern for where container tests actually execute.

`LocalRunner` runs Docker on the host machine — used for dev and anywhere
Docker-in-Docker is available. `GitHubActionsRunner` (coming in step 6)
dispatches a workflow in the pantry runner repo and awaits a callback.

The `run` method is synchronous today: it returns a `ContainerTestResult`
directly. Step 4 changes this to a dispatch-style return where the job
queue pauses in `awaiting_container` status until a callback arrives. For
now we preserve the existing flow so the refactor is behavior-neutral.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..config import get_settings
from .container_test import ContainerTestResult, run_container_tests


class ContainerTestRunner(Protocol):
    async def run(
        self,
        *,
        command_dir: Path,
        submission_id: int,
        packages: list[str] | None,
        is_bundle: bool,
        repo_url: str,
    ) -> ContainerTestResult: ...


class LocalRunner:
    """Runs tests in Docker on the current host."""

    async def run(
        self,
        *,
        command_dir: Path,
        submission_id: int,
        packages: list[str] | None,
        is_bundle: bool,
        repo_url: str,
    ) -> ContainerTestResult:
        return await run_container_tests(
            command_dir=command_dir,
            submission_id=submission_id,
            packages=packages,
            is_bundle=is_bundle,
        )


def get_runner() -> ContainerTestRunner:
    backend = get_settings().container_runner
    if backend == "local":
        return LocalRunner()
    raise ValueError(
        f"Unknown PANTRY_CONTAINER_RUNNER={backend!r}. Expected one of: local",
    )
