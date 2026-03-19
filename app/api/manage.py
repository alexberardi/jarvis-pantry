"""Auth and command management endpoints.

Provides GitHub OAuth login flow and owner-only command operations
(delete, update).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import exchange_github_code, validate_github_token
from ..config import get_settings
from ..db import get_db
from ..models import Author, Command, CommandVersion, Review, SecurityReport, Submission

router = APIRouter()


# ── GitHub OAuth ─────────────────────────────────────────────────────────


@router.get("/v1/auth/github")
def github_auth_url(redirect_uri: str = Query(...)):
    """Return the GitHub OAuth authorization URL.

    The frontend redirects the user here. After auth, GitHub redirects back
    to redirect_uri with ?code=...
    """
    settings = get_settings()
    if not settings.github_client_id:
        raise HTTPException(503, "GitHub OAuth not configured")

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=read:user,public_repo"
    )
    return {"url": url}


class GitHubCallbackRequest(BaseModel):
    code: str


@router.post("/v1/auth/github/callback")
async def github_callback(body: GitHubCallbackRequest):
    """Exchange GitHub OAuth code for access token + user info.

    Returns the token for the frontend to store and use in subsequent requests.
    """
    result = await exchange_github_code(body.code)
    return {
        "access_token": result["access_token"],
        "user": {
            "github_id": result["github_id"],
            "github_username": result["github_username"],
            "display_name": result["display_name"],
            "avatar_url": result["avatar_url"],
        },
    }


@router.get("/v1/auth/me")
def get_current_user(
    author: Author = Depends(validate_github_token),
):
    """Return the current authenticated user's info."""
    return {
        "github_id": author.github_id,
        "github_username": author.github_username,
        "display_name": author.display_name,
        "avatar_url": author.avatar_url,
    }


# ── Command management (owner-only) ─────────────────────────────────────


@router.delete("/v1/commands/{command_name}")
def delete_command(
    command_name: str,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Delete a command. Only the owning author can delete.

    Cascades: removes versions, security reports, submissions, and reviews.
    """
    cmd = db.query(Command).filter(Command.command_name == command_name).first()
    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    if cmd.author_id != author.id:
        raise HTTPException(403, "You can only delete your own commands")

    # Cascade delete related records
    # Security reports (referenced by command_versions.security_report_id)
    versions = db.query(CommandVersion).filter(CommandVersion.command_id == cmd.id).all()
    report_ids = [v.security_report_id for v in versions if v.security_report_id]
    db.query(CommandVersion).filter(CommandVersion.command_id == cmd.id).delete()
    if report_ids:
        db.query(SecurityReport).filter(SecurityReport.id.in_(report_ids)).delete(synchronize_session=False)
    # Also delete reports linked by command_id (may not be linked to a version)
    db.query(SecurityReport).filter(SecurityReport.command_id == cmd.id).delete()
    db.query(Review).filter(Review.command_id == cmd.id).delete()
    db.query(Submission).filter(Submission.command_id == cmd.id).delete()

    db.delete(cmd)
    db.commit()

    return {"status": "deleted", "command_name": command_name}
