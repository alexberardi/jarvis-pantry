"""GitHub repository operations — clone, validate structure, parse manifest."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


class RepoValidationError(Exception):
    """Error validating a command repository."""


def clone_repo(repo_url: str, tag: str | None = None) -> Path:
    """Shallow clone a GitHub repo.

    Args:
        repo_url: HTTPS GitHub URL.
        tag: Optional git tag to checkout.

    Returns:
        Path to the cloned repo directory.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-store-"))
    cmd = ["git", "clone", "--depth", "1"]
    if tag:
        cmd.extend(["--branch", tag])
    cmd.extend([repo_url, str(tmpdir / "repo")])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RepoValidationError(f"git clone failed: {result.stderr.strip()}")

    return tmpdir / "repo"


def validate_structure(repo_dir: Path) -> dict[str, Any]:
    """Validate that a repo has required files and a valid manifest.

    Returns:
        Parsed manifest as dict.
    """
    required = ["jarvis_command.yaml", "command.py", "README.md", "LICENSE"]
    missing = [f for f in required if not (repo_dir / f).exists()]
    if missing:
        raise RepoValidationError(f"Missing required files: {', '.join(missing)}")

    # Parse manifest
    manifest_path = repo_dir / "jarvis_command.yaml"
    try:
        with open(manifest_path) as f:
            manifest = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RepoValidationError(f"Invalid YAML in manifest: {e}")

    if not isinstance(manifest, dict):
        raise RepoValidationError("Manifest must be a YAML mapping")

    # Required manifest fields
    for field in ["name", "description", "version"]:
        if not manifest.get(field):
            raise RepoValidationError(f"Manifest missing required field: {field}")

    return manifest


def get_latest_tag(repo_url: str) -> str | None:
    """Get the latest semver tag from a GitHub repo (without cloning).

    Returns:
        Latest tag string, or None if no tags.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", repo_url],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        ref = line.split("\t")[-1]
        tag = ref.replace("refs/tags/", "").rstrip("^{}")
        if tag and not tag.endswith("^{}"):
            return tag

    return None


def read_command_source(repo_dir: Path) -> str:
    """Read the command.py source code for AI review."""
    cmd_path = repo_dir / "command.py"
    return cmd_path.read_text(encoding="utf-8")


def cleanup_repo(repo_dir: Path) -> None:
    """Remove a cloned repo's temp directory."""
    # repo_dir is tmpdir/repo, so delete the parent
    parent = repo_dir.parent
    if parent.name.startswith("jarvis-store-"):
        shutil.rmtree(parent, ignore_errors=True)
