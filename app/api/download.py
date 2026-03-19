"""Download endpoint for command installations."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Command, CommandVersion
from ..rate_limiter import rate_limiter

router = APIRouter()


@router.get("/v1/commands/{command_name}/download")
def download_command(
    command_name: str,
    request: Request,
    version: str | None = None,
    db: Session = Depends(get_db),
):
    """Get download info for a command (clone URL + tag).

    Public endpoint. Increments install count on each call.
    Rate-limited by client IP.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.check(f"download:{client_ip}"):
        raise HTTPException(429, "Rate limit exceeded")

    cmd = db.query(Command).filter(
        Command.command_name == command_name,
        Command.published.is_(True),
    ).first()

    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    # Get specific or latest version
    if version:
        ver = db.query(CommandVersion).filter(
            CommandVersion.command_id == cmd.id,
            CommandVersion.version == version,
        ).first()
        if not ver:
            raise HTTPException(404, f"Version '{version}' not found")
    else:
        ver = db.query(CommandVersion).filter(
            CommandVersion.command_id == cmd.id,
        ).order_by(CommandVersion.published_at.desc()).first()
        if not ver:
            raise HTTPException(404, "No versions published")

    # Increment install count
    cmd.install_count = (cmd.install_count or 0) + 1
    db.commit()

    return {
        "command_name": cmd.command_name,
        "github_repo_url": cmd.github_repo_url,
        "version": ver.version,
        "git_tag": ver.git_tag,
        "manifest": ver.manifest_json,
        "danger_rating": ver.danger_rating,
        "verified": cmd.verified,
    }
