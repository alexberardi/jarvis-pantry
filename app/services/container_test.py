"""Container test service — runs commands in Docker for safety validation.

Server-side adaptation of jarvis-node-setup/services/container_test_service.py.
Builds a Docker container with the command and jarvis-command-sdk, then runs
test_harness.py inside it with strict resource and network limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

# Test harness lives alongside this file
HARNESS_SCRIPT = Path(__file__).resolve().parent / "test_harness.py"


@dataclass
class ContainerTestResult:
    """Result of running container tests on a command."""

    passed: bool
    summary: str
    test_count: int
    pass_count: int
    fail_count: int
    errors: list[str] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "test_count": self.test_count,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "errors": self.errors,
        }


BASE_IMAGE_TAG = "jarvis-cmd-test-base:latest"
_base_image_built = False


def _ensure_base_image(sdk_path: Path) -> bool:
    """Build the base image with SDK + pyyaml once, reuse for all tests.

    Returns True if base image is available.
    """
    global _base_image_built
    if _base_image_built:
        return True

    # Check if already exists
    result = subprocess.run(
        ["docker", "image", "inspect", BASE_IMAGE_TAG],
        capture_output=True, timeout=10,
    )
    if result.returncode == 0:
        _base_image_built = True
        return True

    # Build it
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-base-"))
    try:
        shutil.copytree(sdk_path, tmpdir / "sdk", ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".venv", ".git", ".pytest_cache",
        ))
        (tmpdir / "Dockerfile").write_text("""\
FROM python:3.11-slim
WORKDIR /test
RUN pip install --no-cache-dir pyyaml
COPY sdk/ /test/sdk/
RUN pip install --no-cache-dir /test/sdk/ && rm -rf /test/sdk/
""")
        build = subprocess.run(
            ["docker", "build", "-t", BASE_IMAGE_TAG, "."],
            cwd=tmpdir, capture_output=True, text=True, timeout=120,
        )
        if build.returncode == 0:
            _base_image_built = True
            return True
        logger.error("Base image build failed: %s", build.stderr[:500])
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _build_dockerfile(packages: list[str], is_bundle: bool = False) -> str:
    """Generate a Dockerfile for testing a command or bundle.

    Uses pre-built base image with SDK already installed.
    """
    pip_install = ""
    if packages:
        pip_install = f'RUN pip install --no-cache-dir {" ".join(packages)}'

    if is_bundle:
        return f"""\
FROM {BASE_IMAGE_TAG}
COPY repo/ /test/repo/
COPY test_harness.py /test/
{pip_install}
CMD ["python", "test_harness.py"]
"""
    else:
        return f"""\
FROM {BASE_IMAGE_TAG}
COPY command_files/ /test/command/
COPY test_harness.py /test/
{pip_install}
CMD ["python", "test_harness.py"]
"""


def _check_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_routine_only(command_dir: Path) -> bool:
    """Check if a package contains only routine components (no Python code)."""
    import yaml
    for name in ("jarvis_package.yaml", "jarvis_command.yaml"):
        manifest_path = command_dir / name
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest = yaml.safe_load(f)
                components = manifest.get("components", [])
                if not components:
                    # Inferred — check for routine.json at root or in routines/
                    has_routine = (command_dir / "routine.json").exists()
                    has_code = any(
                        (command_dir / f).exists()
                        for f in ("command.py", "agent.py", "protocol.py", "manager.py")
                    )
                    return has_routine and not has_code
                return all(c.get("type") == "routine" for c in components)
            except Exception:
                return False
    return False


async def run_container_tests(
    command_dir: Path,
    submission_id: int,
    packages: list[str] | None = None,
    is_bundle: bool = False,
) -> ContainerTestResult:
    """Run container tests on a command directory.

    Args:
        command_dir: Path to directory containing command.py and manifest.
        submission_id: Unique ID for the image tag.
        packages: Additional pip packages to install in the container.

    Returns:
        ContainerTestResult with test outcomes.
    """
    # Skip container tests for routine-only packages (pure JSON, no code to sandbox)
    if _is_routine_only(command_dir):
        logger.info("Routine-only package, skipping container tests")
        return ContainerTestResult(
            passed=True,
            summary="SKIP - routine package (no code)",
            test_count=0,
            pass_count=0,
            fail_count=0,
        )

    if not await asyncio.to_thread(_check_docker_available):
        logger.warning("Docker not available, skipping container tests")
        return ContainerTestResult(
            passed=True,
            summary="SKIP - Docker not available",
            test_count=0,
            pass_count=0,
            fail_count=0,
        )

    settings = get_settings()
    sdk_path = Path(settings.sdk_path)
    timeout = settings.container_test_timeout

    if not sdk_path.exists():
        logger.warning("SDK path %s not found, skipping container tests", sdk_path)
        return ContainerTestResult(
            passed=True,
            summary="SKIP - SDK not available",
            test_count=0,
            pass_count=0,
            fail_count=0,
        )

    # Ensure base image with SDK is built (cached across submissions)
    base_ready = await asyncio.to_thread(_ensure_base_image, sdk_path)
    if not base_ready:
        logger.warning("Could not build base image, skipping container tests")
        return ContainerTestResult(
            passed=True,
            summary="SKIP - Base image build failed",
            test_count=0,
            pass_count=0,
            fail_count=0,
        )

    packages = packages or []
    image_tag = f"jarvis-cmd-test:{submission_id}"
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-test-"))

    try:
        # Set up build context (SDK already in base image — just copy test files)
        context_dir = tmpdir / "context"
        context_dir.mkdir()

        # Copy command/repo files
        if is_bundle:
            # Bundle: copy entire repo preserving directory structure
            repo_dest = context_dir / "repo"
            shutil.copytree(command_dir, repo_dest, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".git", ".venv", ".pytest_cache",
            ))
        else:
            # Single command: copy files flat
            cmd_dest = context_dir / "command_files"
            cmd_dest.mkdir()
            for f in command_dir.iterdir():
                if f.suffix == ".py" or f.name in ("jarvis_command.yaml", "jarvis_package.yaml"):
                    shutil.copy2(f, cmd_dest / f.name)

        # Copy test harness
        shutil.copy2(HARNESS_SCRIPT, context_dir / "test_harness.py")

        # Write Dockerfile
        dockerfile = _build_dockerfile(packages, is_bundle=is_bundle)
        (context_dir / "Dockerfile").write_text(dockerfile)

        # Build image (blocking → thread)
        build_result = await asyncio.to_thread(
            subprocess.run,
            ["docker", "build", "-t", image_tag, "."],
            cwd=context_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if build_result.returncode != 0:
            return ContainerTestResult(
                passed=False,
                summary="FAIL - Docker build failed",
                test_count=0,
                pass_count=0,
                fail_count=1,
                errors=[build_result.stderr],
                raw_output=build_result.stdout + build_result.stderr,
            )

        # Run container with strict limits (blocking → thread)
        run_cmd = [
            "docker", "run",
            "--rm",
            "--network=none",
            "--memory=128m",
            "--cpus=0.5",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=32m",
            image_tag,
        ]

        run_result = await asyncio.to_thread(
            subprocess.run,
            run_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Parse output
        try:
            test_results = json.loads(run_result.stdout)
            return ContainerTestResult(
                passed=test_results.get("failed", 1) == 0,
                summary=test_results.get("summary", "Unknown"),
                test_count=test_results.get("passed", 0) + test_results.get("failed", 0),
                pass_count=test_results.get("passed", 0),
                fail_count=test_results.get("failed", 0),
                errors=test_results.get("errors", []),
                raw_output=run_result.stdout,
            )
        except json.JSONDecodeError:
            return ContainerTestResult(
                passed=False,
                summary="FAIL - Could not parse test output",
                test_count=0,
                pass_count=0,
                fail_count=1,
                errors=[run_result.stdout, run_result.stderr],
                raw_output=run_result.stdout + run_result.stderr,
            )

    except subprocess.TimeoutExpired:
        return ContainerTestResult(
            passed=False,
            summary=f"FAIL - Container timed out after {timeout}s",
            test_count=0,
            pass_count=0,
            fail_count=1,
            errors=["Container exceeded timeout"],
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Clean up image
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rmi", "-f", image_tag],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass
