"""Forge draft endpoints — share codes for test-installing AI-generated packages.

A draft is created/updated on each Forge generate call. The share code lets
users download the current package state to test on a node before publishing.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ForgeDraft

router = APIRouter()
logger = logging.getLogger(__name__)

# Unambiguous characters (no 0/O, 1/I/L)
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_EXPIRY_MINUTES = 15
_MAX_RETRIES = 5


def _generate_share_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


# =============================================================================
# Request/Response Models
# =============================================================================


class ForgeDraftUpsertRequest(BaseModel):
    session_id: str
    package_name: str
    display_name: str | None = None
    files: list[dict[str, str]]  # [{filename, content, language}]


class ForgeDraftUpsertResponse(BaseModel):
    share_code: str
    session_id: str
    expires_at: datetime


class ForgeDraftFile(BaseModel):
    filename: str
    content: str
    language: str


class ForgeDraftGetResponse(BaseModel):
    package_name: str
    display_name: str | None
    files: list[ForgeDraftFile]
    expires_at: datetime


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/v1/forge/drafts", response_model=ForgeDraftUpsertResponse)
def upsert_draft(
    body: ForgeDraftUpsertRequest,
    db: Session = Depends(get_db),
) -> ForgeDraftUpsertResponse:
    """Create or update a Forge draft. Returns a share code.

    If a draft with this session_id already exists, updates it and refreshes
    the expiry. Otherwise creates a new draft with a fresh share code.
    """
    if not body.files:
        raise HTTPException(400, "At least one file is required")
    if not body.package_name.strip():
        raise HTTPException(400, "Package name is required")

    # Warn (log) if README.md is missing — required for Pantry submission
    filenames = {f.get("filename", "").lower() for f in body.files}
    if "readme.md" not in filenames:
        logger.warning(
            "Forge draft '%s' is missing README.md — required for Pantry submission",
            body.package_name,
        )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=_EXPIRY_MINUTES)

    existing = db.query(ForgeDraft).filter(
        ForgeDraft.session_id == body.session_id,
    ).first()

    if existing:
        existing.package_name = body.package_name.strip()
        existing.display_name = body.display_name
        existing.files_json = body.files
        existing.expires_at = expires_at
        db.commit()

        logger.info(
            "Forge draft updated: session=%s code=%s",
            body.session_id[:8], existing.share_code,
        )
        return ForgeDraftUpsertResponse(
            share_code=existing.share_code,
            session_id=existing.session_id,
            expires_at=existing.expires_at,
        )

    # Create new draft with unique share code
    for _ in range(_MAX_RETRIES):
        code = _generate_share_code()
        conflict = db.query(ForgeDraft).filter(
            ForgeDraft.share_code == code,
        ).first()
        if not conflict:
            break
    else:
        raise HTTPException(500, "Failed to generate unique share code")

    draft = ForgeDraft(
        share_code=code,
        session_id=body.session_id,
        package_name=body.package_name.strip(),
        display_name=body.display_name,
        files_json=body.files,
        expires_at=expires_at,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)

    logger.info(
        "Forge draft created: session=%s code=%s package=%s",
        body.session_id[:8], code, body.package_name,
    )
    return ForgeDraftUpsertResponse(
        share_code=draft.share_code,
        session_id=draft.session_id,
        expires_at=draft.expires_at,
    )


@router.get("/v1/forge/drafts/{share_code}", response_model=ForgeDraftGetResponse)
def get_draft(
    share_code: str,
    db: Session = Depends(get_db),
) -> ForgeDraftGetResponse:
    """Retrieve a Forge draft by share code. Returns 404 if expired or not found."""
    draft = db.query(ForgeDraft).filter(
        ForgeDraft.share_code == share_code.upper(),
    ).first()

    if not draft:
        raise HTTPException(404, "Share code not found")

    now = datetime.now(timezone.utc)
    if draft.expires_at < now:
        raise HTTPException(404, "Share code expired")

    files = [
        ForgeDraftFile(
            filename=f.get("filename", ""),
            content=f.get("content", ""),
            language=f.get("language", ""),
        )
        for f in (draft.files_json or [])
    ]

    return ForgeDraftGetResponse(
        package_name=draft.package_name,
        display_name=draft.display_name,
        files=files,
        expires_at=draft.expires_at,
    )


@router.delete("/v1/forge/drafts/{session_id}")
def delete_draft(
    session_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Delete a Forge draft by session ID. Used on publish or session close."""
    count = db.query(ForgeDraft).filter(
        ForgeDraft.session_id == session_id,
    ).delete()
    db.commit()

    if count == 0:
        raise HTTPException(404, "Draft not found")

    return {"status": "deleted"}


# =============================================================================
# Cleanup
# =============================================================================


def cleanup_expired_drafts(db: Session) -> int:
    """Delete all expired forge drafts. Returns count deleted."""
    now = datetime.now(timezone.utc)
    count = db.query(ForgeDraft).filter(ForgeDraft.expires_at < now).delete()
    db.commit()
    return count
