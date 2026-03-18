"""Command submission and submission status endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import validate_github_token
from ..db import get_db
from ..models import Author, Command, CommandVersion, Submission
from ..services.submission_pipeline import process_submission
from ..services.github_service import (
    RepoValidationError,
    clone_repo,
    validate_structure,
    cleanup_repo,
)

router = APIRouter()


class SubmitRequest(BaseModel):
    repo_url: str
    llm_provider: str = "claude"  # "claude" or "openai"
    llm_api_key: str


@router.post("/v1/commands")
async def submit_command(
    body: SubmitRequest,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Submit a command for review and publication.

    Requires GitHub OAuth token. The submitter provides their own LLM API key
    for the AI security review (BYOK).
    """
    # Validate provider
    if body.llm_provider not in ("claude", "openai"):
        raise HTTPException(400, "llm_provider must be 'claude' or 'openai'")

    # Validate repo URL
    if not body.repo_url.startswith("https://github.com/"):
        raise HTTPException(400, "repo_url must be a public GitHub HTTPS URL")

    try:
        result = await process_submission(
            repo_url=body.repo_url,
            llm_provider=body.llm_provider,
            llm_api_key=body.llm_api_key,
            author=author,
            db=db,
        )
        return result
    except RepoValidationError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(502, f"AI review failed: {e}")


@router.get("/v1/submissions/{submission_id}")
def get_submission(
    submission_id: int,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Check status of a submission."""
    submission = db.query(Submission).filter(
        Submission.id == submission_id,
        Submission.author_id == author.id,
    ).first()

    if not submission:
        raise HTTPException(404, "Submission not found")

    return {
        "id": submission.id,
        "status": submission.status,
        "github_repo_url": submission.github_repo_url,
        "error_message": submission.error_message,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
        "completed_at": submission.completed_at.isoformat() if submission.completed_at else None,
    }


class QuickSubmitRequest(BaseModel):
    repo_url: str
    author_github: str = "community"


@router.post("/v1/commands/quick-submit")
def quick_submit(
    body: QuickSubmitRequest,
    db: Session = Depends(get_db),
):
    """Submit a command by GitHub URL — no OAuth, no AI review.

    Clones the repo, validates structure + manifest, and publishes directly.
    """
    repo_url = body.repo_url.rstrip("/")
    if not repo_url.startswith("https://github.com/"):
        raise HTTPException(400, "repo_url must be a public GitHub HTTPS URL")

    repo_dir = None
    try:
        # 1. Clone
        repo_dir = clone_repo(repo_url)

        # 2. Validate
        manifest = validate_structure(repo_dir)
        command_name = manifest["name"]
        version = manifest.get("version", "0.1.0")

        # 3. Find or create author
        author = db.query(Author).filter(
            Author.github_username == body.author_github
        ).first()
        if not author:
            author = Author(
                github_id=0,
                github_username=body.author_github,
                display_name=body.author_github,
            )
            db.add(author)
            db.flush()

        # 4. Upsert command
        existing = db.query(Command).filter(
            Command.command_name == command_name
        ).first()

        if existing:
            existing.description = manifest.get("description", existing.description)
            existing.display_name = manifest.get("display_name", existing.display_name)
            existing.github_repo_url = repo_url
            existing.categories = manifest.get("categories", [])
            existing.platforms = manifest.get("platforms", [])
            existing.license = manifest.get("license")
            existing.latest_version = version
            existing.published = True
            command = existing
        else:
            command = Command(
                command_name=command_name,
                display_name=manifest.get("display_name", command_name),
                description=manifest.get("description", ""),
                github_repo_url=repo_url,
                author_id=author.id,
                latest_version=version,
                categories=manifest.get("categories", []),
                platforms=manifest.get("platforms", []),
                license=manifest.get("license", "MIT"),
                published=True,
            )
            db.add(command)
            db.flush()

        # 5. Add version
        existing_ver = db.query(CommandVersion).filter(
            CommandVersion.command_id == command.id,
            CommandVersion.version == version,
        ).first()
        if not existing_ver:
            cmd_version = CommandVersion(
                command_id=command.id,
                version=version,
                git_tag=f"v{version}",
                manifest_json=manifest,
                min_jarvis_version=manifest.get("min_jarvis_version", "0.9.0"),
            )
            db.add(cmd_version)

        db.commit()

        return {
            "status": "published",
            "command_name": command_name,
            "display_name": manifest.get("display_name", command_name),
            "version": version,
            "description": manifest.get("description", ""),
        }

    except RepoValidationError as e:
        raise HTTPException(422, str(e))

    finally:
        if repo_dir:
            cleanup_repo(repo_dir)
