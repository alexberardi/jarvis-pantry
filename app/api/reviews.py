"""Review endpoints — list and submit reviews for commands."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import validate_github_token
from ..db import get_db
from ..models import Author, Command, Review

router = APIRouter()


class ReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    title: str = Field(..., max_length=255)
    body: str = ""


@router.get("/v1/commands/{command_name}/reviews")
def list_reviews(
    command_name: str,
    db: Session = Depends(get_db),
):
    """List reviews for a command."""
    cmd = db.query(Command).filter(
        Command.command_name == command_name,
        Command.published.is_(True),
    ).first()

    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    reviews = db.query(Review).filter(Review.command_id == cmd.id).all()

    return {
        "command_name": command_name,
        "reviews": [
            {
                "author": r.author.github_username if r.author else None,
                "rating": r.rating,
                "title": r.title,
                "body": r.body,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews
        ],
    }


@router.post("/v1/commands/{command_name}/reviews")
def submit_review(
    command_name: str,
    body: ReviewRequest,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Submit a review for a command. One review per user per command."""
    cmd = db.query(Command).filter(
        Command.command_name == command_name,
        Command.published.is_(True),
    ).first()

    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    # Check for existing review
    existing = db.query(Review).filter(
        Review.command_id == cmd.id,
        Review.author_id == author.id,
    ).first()

    if existing:
        # Update existing review
        existing.rating = body.rating
        existing.title = body.title
        existing.body = body.body
        db.commit()
        return {"status": "updated", "review_id": existing.id}

    # Create new review
    review = Review(
        command_id=cmd.id,
        author_id=author.id,
        rating=body.rating,
        title=body.title,
        body=body.body,
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    return {"status": "created", "review_id": review.id}


@router.post("/v1/admin/commands/{command_name}/verify")
def verify_command(
    command_name: str,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Mark a command as verified (admin-only).

    Requires X-Admin-Key header matching the ADMIN_API_KEY env var.
    """
    from ..config import get_settings
    settings = get_settings()
    expected_key = getattr(settings, "admin_api_key", "")
    if not expected_key or x_admin_key != expected_key:
        raise HTTPException(403, "Invalid admin key")

    cmd = db.query(Command).filter(Command.command_name == command_name).first()
    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    cmd.verified = True
    db.commit()

    return {"status": "verified", "command_name": command_name}


@router.post("/v1/admin/commands/{command_name}/unpublish")
def unpublish_command(
    command_name: str,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Unpublish a command (admin-only)."""
    from ..config import get_settings
    settings = get_settings()
    expected_key = getattr(settings, "admin_api_key", "")
    if not expected_key or x_admin_key != expected_key:
        raise HTTPException(403, "Invalid admin key")

    cmd = db.query(Command).filter(Command.command_name == command_name).first()
    if not cmd:
        raise HTTPException(404, f"Command '{command_name}' not found")

    cmd.published = False
    db.commit()

    return {"status": "unpublished", "command_name": command_name}
