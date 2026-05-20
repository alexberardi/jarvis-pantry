"""Command detail and version listing endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Command, CommandVersion, Review, SecurityReport

router = APIRouter()


@router.get("/v1/commands/{command_name}")
def get_command(command_name: str, db: Session = Depends(get_db)):
    """Get full details for a command."""
    cmd = db.query(Command).filter(
        Command.command_name == command_name,
        Command.published.is_(True),
    ).first()

    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    # Get latest security report
    latest_version = db.query(CommandVersion).filter(
        CommandVersion.command_id == cmd.id,
    ).order_by(CommandVersion.published_at.desc()).first()

    security_report = None
    if latest_version and latest_version.security_report_id:
        report = db.query(SecurityReport).get(latest_version.security_report_id)
        if report:
            security_report = {
                "summary": report.ai_review_summary,
                "danger_score": report.ai_danger_score,
                "concerns": report.ai_concerns or [],
                "recommendation": report.ai_recommendation,
                "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
            }

    apt_packages: list[str] = []
    if latest_version and isinstance(latest_version.manifest_json, dict):
        raw_apt = latest_version.manifest_json.get("apt_packages")
        if isinstance(raw_apt, list):
            apt_packages = [str(p) for p in raw_apt if isinstance(p, str)]

    # Get reviews
    reviews = db.query(Review).filter(Review.command_id == cmd.id).all()
    avg_rating = sum(r.rating for r in reviews) / len(reviews) if reviews else None

    return {
        "command_name": cmd.command_name,
        "display_name": cmd.display_name,
        "description": cmd.description,
        "github_repo_url": cmd.github_repo_url,
        "author": {
            "github": cmd.author.github_username,
            "display_name": cmd.author.display_name,
            "avatar_url": cmd.author.avatar_url,
        } if cmd.author else None,
        "latest_version": cmd.latest_version,
        "categories": cmd.categories or [],
        "platforms": cmd.platforms or [],
        "license": cmd.license,
        "install_count": cmd.install_count,
        "danger_rating": cmd.danger_rating,
        "verified": cmd.verified,
        "icon_url": cmd.icon_url,
        "package_type": cmd.package_type or "command",
        "components": cmd.components or [],
        "apt_packages": apt_packages,
        "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
        "updated_at": cmd.updated_at.isoformat() if cmd.updated_at else None,
        "security_report": security_report,
        "review_count": len(reviews),
        "avg_rating": round(avg_rating, 1) if avg_rating else None,
    }


@router.get("/v1/commands/{command_name}/versions")
def list_versions(command_name: str, db: Session = Depends(get_db)):
    """List all versions of a command."""
    cmd = db.query(Command).filter(
        Command.command_name == command_name,
        Command.published.is_(True),
    ).first()

    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    versions = db.query(CommandVersion).filter(
        CommandVersion.command_id == cmd.id,
    ).order_by(CommandVersion.published_at.desc()).all()

    return {
        "command_name": command_name,
        "versions": [
            {
                "version": v.version,
                "git_tag": v.git_tag,
                "danger_rating": v.danger_rating,
                "min_jarvis_version": v.min_jarvis_version,
                "published_at": v.published_at.isoformat() if v.published_at else None,
            }
            for v in versions
        ],
    }
