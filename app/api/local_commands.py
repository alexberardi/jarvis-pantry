"""Add a package to this Pantry straight from a Git repository, without the public submission pipeline.

For a household's private integrations. There is no AI security review and no container test here, so it is
admin-only and refuses to run at all unless ``ADMIN_API_KEY`` is set: whatever is added is installed by nodes.
"""

from __future__ import annotations

import logging
import shutil
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from .admin_key import require_admin_key
from ..models import Author, Command, CommandVersion
from ..services.github_service import RepoValidationError, clone_repo, validate_structure

router = APIRouter()
logger = logging.getLogger(__name__)

LOCAL_AUTHOR = "_local"  # stored username of the local author; GitHub logins cannot start with an underscore


class AddFromRepoRequest(BaseModel):
    github_repo_url: str
    ref: str | None = None      # tag or branch; default branch when omitted
    token: str | None = None    # for a private repository; PANTRY_GITHUB_TOKEN is used when omitted


def _require_admin(x_admin_key: str | None) -> None:
    require_admin_key(x_admin_key, "Adding packages from a repository")


def _clone_url(repo_url: str, token: str) -> str:
    """Embed a token for the clone only; the stored repository URL never carries it."""
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(400, "Repository URL must be https://")
    if not token:
        return repo_url
    return urlunparse(parsed._replace(netloc=f"x-access-token:{token}@{parsed.netloc.split('@')[-1]}"))


@router.post("/v1/admin/commands/from-repo")
def add_from_repo(
    body: AddFromRepoRequest,
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Clone a repository, validate its manifest and publish it as a local (private) package."""
    from ..config import get_settings

    _require_admin(x_admin_key)
    repo_url = body.github_repo_url.strip().rstrip("/")
    token = (body.token or getattr(get_settings(), "pantry_github_token", "") or "").strip()
    clone_url = _clone_url(repo_url, token)

    repo_dir = None
    try:
        repo_dir, commit_sha = clone_repo(clone_url, body.ref)
        manifest: dict[str, Any] = validate_structure(repo_dir)
    except RepoValidationError as e:
        # The clone error can echo the URL; never let a token reach the response.
        raise HTTPException(400, str(e).replace(token, "***") if token else str(e))
    finally:
        if repo_dir is not None:
            shutil.rmtree(repo_dir.parent, ignore_errors=True)

    name = manifest["name"]
    version = str(manifest.get("version", "0.1.0"))
    components = manifest.get("components", [])
    is_bundle = len(components) > 1 or (
        len(components) == 1 and components[0].get("type") != "command"
    ) or (len(components) == 1 and "/" in components[0].get("path", ""))
    package_type = "bundle" if is_bundle else "command"

    author = db.query(Author).filter(Author.is_local.is_(True)).first()
    if not author:
        author = Author(github_id=-1, github_username=LOCAL_AUTHOR, display_name="Local (private)", is_local=True)
        db.add(author)
        db.flush()

    command = db.query(Command).filter(Command.command_name == name).first()
    # This route is administrator-only, so it deliberately does not go through may_publish_to(): the administrator may
    # update any package this route created. The check below enforces the same rule from the other side, refusing to
    # touch a package that is not owned by the local author.
    if command is not None and not (command.author and command.author.is_local):
        # Never repoint a package that came through the public submission pipeline at a repository added here:
        # it would silently replace reviewed code (and keep its author, verified badge and install count).
        raise HTTPException(
            409,
            f"A package named '{name}' already exists and was not added from a repository, so it is not replaced. "
            "Rename the package in the manifest, or unpublish the existing one first.",
        )
    if command is None:
        command = Command(command_name=name, author_id=author.id, install_count=0)
        db.add(command)
    command.display_name = manifest.get("display_name", name)
    command.description = manifest.get("description", "")
    command.github_repo_url = repo_url
    command.latest_version = version
    command.categories = manifest.get("categories", [])
    command.platforms = manifest.get("platforms", [])
    command.license = manifest.get("license", "MIT")
    command.package_type = package_type
    command.components = components or None
    command.published = True
    db.flush()

    row = db.query(CommandVersion).filter(
        CommandVersion.command_id == command.id, CommandVersion.version == version,
    ).first()
    if row is None:
        row = CommandVersion(command_id=command.id, version=version)
        db.add(row)
    row.git_tag = body.ref or f"v{version}"
    row.git_commit_sha = commit_sha
    row.manifest_json = manifest
    row.min_jarvis_version = manifest.get("min_jarvis_version", "0.9.0")
    db.commit()

    logger.info("Added local package %s %s from %s at %s", name, version, repo_url, commit_sha)
    return {
        "command_name": name,
        "version": version,
        "package_type": package_type,
        "git_commit_sha": commit_sha,
        "private_repository": bool(token),
        "review": "none: added by the administrator without the Pantry's security review or container tests",
    }


@router.delete("/v1/admin/commands/{command_name}/local")
def remove_local_command(
    command_name: str,
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Unpublish a package that was added from a repository (it is hidden, not deleted)."""
    _require_admin(x_admin_key)
    command = db.query(Command).filter(Command.command_name == command_name).first()
    if command is None or not (command.author and command.author.is_local):
        raise HTTPException(404, f"No local package '{command_name}'")
    command.published = False
    db.commit()
    return {"command_name": command_name, "published": False}
