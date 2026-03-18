"""GitHub repository operations — clone, validate structure, parse manifest."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
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


def _find_manifest_path(repo_dir: Path) -> Path:
    """Find the manifest file, preferring jarvis_package.yaml over jarvis_command.yaml."""
    for name in ("jarvis_package.yaml", "jarvis_command.yaml"):
        path = repo_dir / name
        if path.exists():
            return path
    raise RepoValidationError("Missing manifest: neither jarvis_package.yaml nor jarvis_command.yaml found")


def validate_structure(repo_dir: Path) -> dict[str, Any]:
    """Validate that a repo has required files and a valid manifest.

    Supports both single-command and multi-component bundle repos.

    Returns:
        Parsed manifest as dict.
    """
    # Check common required files
    for f in ("README.md", "LICENSE"):
        if not (repo_dir / f).exists():
            raise RepoValidationError(f"Missing required file: {f}")

    # Parse manifest
    manifest_path = _find_manifest_path(repo_dir)
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

    # Infer components from repo structure if not declared
    components = manifest.get("components", [])
    if not components:
        components = _infer_components_from_structure(repo_dir, manifest["name"])
        if not components:
            raise RepoValidationError(
                "No components found. Declare 'components' in the manifest or use "
                "the convention: command.py, commands/*/command.py, agents/*/agent.py, "
                "device_families/*/protocol.py, device_managers/*/manager.py"
            )
        manifest["components"] = components

    # Validate component paths exist
    for comp in components:
        comp_path = repo_dir / comp.get("path", "")
        if not comp_path.exists():
            raise RepoValidationError(
                f"Component '{comp.get('name', '?')}' path not found: {comp.get('path')}"
            )

    return manifest


def _infer_components_from_structure(repo_dir: Path, manifest_name: str) -> list[dict[str, str]]:
    """Infer components from repo directory structure.

    Scans for:
    - command.py at root → single command
    - commands/<name>/command.py → command(s)
    - agents/<name>/agent.py → agent(s)
    - device_families/<name>/protocol.py → device protocol(s)
    - device_managers/<name>/manager.py → device manager(s)
    """
    dir_types = {
        "commands": ("command", "command.py"),
        "agents": ("agent", "agent.py"),
        "device_families": ("device_protocol", "protocol.py"),
        "device_managers": ("device_manager", "manager.py"),
    }

    components: list[dict[str, str]] = []

    # Root-level command.py
    if (repo_dir / "command.py").exists():
        components.append({"type": "command", "name": manifest_name, "path": "command.py"})

    # Convention directories
    for dir_name, (comp_type, entry_filename) in dir_types.items():
        type_dir = repo_dir / dir_name
        if not type_dir.is_dir():
            continue
        for sub_dir in sorted(type_dir.iterdir()):
            if not sub_dir.is_dir() or sub_dir.name.startswith(("_", ".")):
                continue
            entry_point = sub_dir / entry_filename
            if entry_point.exists():
                components.append({
                    "type": comp_type,
                    "name": sub_dir.name,
                    "path": str(entry_point.relative_to(repo_dir)),
                })

    return components


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


def read_component_sources(repo_dir: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Read source code for all components in a manifest.

    For single-command manifests (no components field), returns {"command.py": <source>}.
    For bundles, reads each component's path.

    Returns:
        Dict mapping component name to source code.
    """
    components = manifest.get("components", [])
    if not components:
        # Shouldn't happen if validate_structure was called, but handle gracefully
        components = _infer_components_from_structure(repo_dir, manifest.get("name", "unknown"))

    sources: dict[str, str] = {}
    for comp in components:
        comp_path = repo_dir / comp["path"]
        if comp_path.exists():
            sources[comp["name"]] = comp_path.read_text(encoding="utf-8")
    return sources


def cleanup_repo(repo_dir: Path) -> None:
    """Remove a cloned repo's temp directory."""
    # repo_dir is tmpdir/repo, so delete the parent
    parent = repo_dir.parent
    if parent.name.startswith("jarvis-store-"):
        shutil.rmtree(parent, ignore_errors=True)


def parse_repo_owner_name(repo_url: str) -> tuple[str, str]:
    """Extract owner and repo name from a GitHub URL.

    Args:
        repo_url: e.g. "https://github.com/owner/repo-name"

    Returns:
        Tuple of (owner, repo_name).

    Raises:
        RepoValidationError: If URL doesn't match expected format.
    """
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url)
    if not match:
        raise RepoValidationError(f"Cannot parse GitHub owner/repo from URL: {repo_url}")
    return match.group(1), match.group(2)


async def verify_repo_access(repo_url: str, github_token: str) -> str:
    """Verify the authenticated user has push access to the repo.

    Args:
        repo_url: GitHub HTTPS URL.
        github_token: User's GitHub access token.

    Returns:
        The repo owner's GitHub username.

    Raises:
        RepoValidationError: If user doesn't have push access or repo not found.
    """
    owner, repo_name = parse_repo_owner_name(repo_url)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo_name}",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15.0,
        )

    if resp.status_code == 404:
        raise RepoValidationError(
            f"Repository not found or not accessible: {owner}/{repo_name}"
        )
    if resp.status_code != 200:
        raise RepoValidationError(
            f"GitHub API error checking repo access: {resp.status_code}"
        )

    data = resp.json()
    permissions = data.get("permissions", {})

    if not permissions.get("push", False):
        raise RepoValidationError(
            f"You don't have push access to {owner}/{repo_name}. "
            "You can only submit repos you own or collaborate on."
        )

    return owner
