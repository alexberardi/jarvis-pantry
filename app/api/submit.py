"""Command submission and submission status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import validate_github_token
from ..db import get_db
from ..models import Author, Submission
from ..services.submission_pipeline import process_submission
from ..services.github_service import RepoValidationError

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
