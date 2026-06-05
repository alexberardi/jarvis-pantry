"""Operator bulk-upload endpoints + dedicated GitHub OAuth flow.

Three surfaces, all for the pantry operator only:

1. `/v1/admin/bulk-submissions` (POST/GET) — admin-key gated; takes a list
   of repos, queues N submissions through the validation pipeline, polls
   the batch as a set.
2. `/v1/operator/auth/github*` — a parallel OAuth flow that uses the
   separate `PANTRY_OPERATOR_GITHUB_CLIENT_*` credentials. The operator
   page can't share the regular submission flow's OAuth app because
   GitHub OAuth Apps only allow one callback URL — see the panel doc on
   the operator HTML for the trade-off.

The operator HTML at `/operator/` and the `scripts/bulk_submit.py` CLI
both target these endpoints.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import exchange_github_code
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import Author, Command, Submission
from ..services.github_service import (
    RepoValidationError,
    cleanup_repo,
    clone_repo,
    validate_structure,
)
from ..services.job_queue import SubmissionJob, validation_queue
from ..services.lockfile_resolver import (
    LockfileResolutionError,
    LockfileTooLargeError,
    resolve_lockfile,
)
from ..services.static_analysis import run_static_analysis

logger = logging.getLogger(__name__)
router = APIRouter()


# Bounded concurrency for parallel clones during a bulk submission. Sized
# from MAX_CONCURRENT_CLONES so the operator path shares throttling intent
# with the public submission path.
_bulk_clone_semaphore = asyncio.Semaphore(get_settings().max_concurrent_clones)


# Sentinel Author for operator-submitted entries. Reused for every
# bulk-submitted Submission row so the operator's curation activity is
# distinguishable from regular author submissions. github_id=0 mirrors the
# pre-OAuth sentinel pattern already used in finalize_submission.
_OPERATOR_GITHUB_USERNAME = "pantry-operator"


def _require_admin_key(x_admin_key: str) -> None:
    settings = get_settings()
    expected = getattr(settings, "admin_api_key", "")
    if not expected or x_admin_key != expected:
        raise HTTPException(403, "Invalid admin key")


def _get_or_create_operator_author(db: Session) -> Author:
    author = (
        db.query(Author)
        .filter(Author.github_username == _OPERATOR_GITHUB_USERNAME)
        .first()
    )
    if author:
        return author
    author = Author(
        github_id=0,
        github_username=_OPERATOR_GITHUB_USERNAME,
        display_name="Pantry Operator",
    )
    db.add(author)
    db.commit()
    db.refresh(author)
    return author


# ── Bulk submit ──────────────────────────────────────────────────────────


class BulkRepoSpec(BaseModel):
    owner_repo: str = Field(
        ...,
        description="GitHub owner/repo (e.g. 'alexberardi/my-command') or full HTTPS URL",
    )


class BulkSubmitRequest(BaseModel):
    repos: list[BulkRepoSpec] = Field(..., min_length=1, max_length=100)
    llm_provider: str = Field("claude", description="'claude' or 'openai'")
    llm_api_key: str = Field(..., min_length=1)


class BulkRepoOutcome(BaseModel):
    owner_repo: str
    accepted: bool
    submission_id: int
    command_name: str | None = None
    version: str | None = None
    reason: str | None = None


class BulkSubmitResponse(BaseModel):
    batch_id: str
    accepted_count: int
    rejected_count: int
    outcomes: list[BulkRepoOutcome]


def _normalize_owner_repo(value: str) -> str:
    """Accept either 'owner/repo' or 'https://github.com/owner/repo'."""
    v = value.strip().rstrip("/")
    if v.startswith("https://github.com/"):
        v = v[len("https://github.com/"):]
    return v


@router.post("/v1/admin/bulk-submissions", response_model=BulkSubmitResponse)
async def bulk_submit(
    body: BulkSubmitRequest,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
):
    """Bulk-submit GitHub repos for AI review + container testing.

    Operator-only (X-Admin-Key gated). Each repo is processed in parallel,
    bounded by MAX_CONCURRENT_CLONES. Pre-validation failures (clone,
    structure, static analysis, lockfile) still create a Submission row
    tagged with the shared batch_id so the operator can poll one endpoint
    to see the whole batch.
    """
    _require_admin_key(x_admin_key)

    if body.llm_provider not in ("claude", "openai"):
        raise HTTPException(400, "llm_provider must be 'claude' or 'openai'")

    # Ensure the operator author exists before fan-out (workers create their
    # own sessions; doing the lookup once up-front avoids a thundering herd
    # of concurrent inserts on first bulk run).
    bootstrap_db = SessionLocal()
    try:
        operator = _get_or_create_operator_author(bootstrap_db)
        operator_id = operator.id
        operator_github = operator.github_username
    finally:
        bootstrap_db.close()

    batch_id = uuid.uuid4().hex

    tasks = [
        _process_one(
            owner_repo=_normalize_owner_repo(spec.owner_repo),
            llm_provider=body.llm_provider,
            llm_api_key=body.llm_api_key,
            author_id=operator_id,
            author_github=operator_github,
            batch_id=batch_id,
        )
        for spec in body.repos
    ]
    raw_outcomes = await asyncio.gather(*tasks)
    outcomes = [BulkRepoOutcome(**o) for o in raw_outcomes]

    accepted = sum(1 for o in outcomes if o.accepted)
    return BulkSubmitResponse(
        batch_id=batch_id,
        accepted_count=accepted,
        rejected_count=len(outcomes) - accepted,
        outcomes=outcomes,
    )


def _record(submission_id: int, **fields: Any) -> None:
    """Update a Submission row in a short-lived session.

    Each bulk task fires off a clone + static analysis that can take many
    seconds; holding the same Session across that gap saw psycopg2
    "server closed the connection unexpectedly" in prod under fan-out
    (lots of concurrent tasks sitting on idle connections). Short sessions
    keep each commit to a sub-second checkout window so the pool stays
    healthy under the same parallelism.
    """
    db: Session = SessionLocal()
    try:
        sub = db.query(Submission).filter(Submission.id == submission_id).first()
        if sub is None:
            return
        for key, value in fields.items():
            setattr(sub, key, value)
        db.commit()
    finally:
        db.close()


async def _process_one(
    *,
    owner_repo: str,
    llm_provider: str,
    llm_api_key: str,
    author_id: int,
    author_github: str,
    batch_id: str,
) -> dict[str, Any]:
    """Process a single repo with short-lived DB sessions.

    Always creates a Submission row tagged with batch_id. Failures resolve
    to status='rejected'; accepted entries transition to 'ai_review' or
    'container_test' and are enqueued on the validation queue. Each DB
    write is its own session — no session is held across the slow clone /
    static-analysis steps (see _record's docstring).
    """
    repo_url = f"https://github.com/{owner_repo}".rstrip("/")

    # Initial row creation — short session.
    db: Session = SessionLocal()
    try:
        submission = Submission(
            github_repo_url=repo_url,
            author_id=author_id,
            status="pending",
            batch_id=batch_id,
            llm_provider=llm_provider if llm_api_key else None,
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)
        submission_id = submission.id
    finally:
        db.close()

    repo_dir = None
    try:
        async with _bulk_clone_semaphore:
            repo_dir = await asyncio.to_thread(clone_repo, repo_url)

        manifest = validate_structure(repo_dir)
        command_name = manifest["name"]
        version = manifest.get("version", "0.1.0")

        analysis = run_static_analysis(repo_dir)
        _record(
            submission_id,
            status="static_analysis",
            static_analysis_result=analysis.to_dict(),
        )

        if not analysis.passed:
            cleanup_repo(repo_dir)
            reason = (
                "Static analysis failed: " + ", ".join(analysis.reason_codes)
                if analysis.reason_codes
                else "Static analysis failed"
            )
            _record(
                submission_id,
                status="rejected",
                error_message=reason,
                completed_at=datetime.now(timezone.utc),
            )
            return {
                "owner_repo": owner_repo,
                "accepted": False,
                "submission_id": submission_id,
                "reason": reason,
            }

        package_specs = [
            p["name"]
            for p in manifest.get("packages", []) or []
            if isinstance(p, dict) and p.get("name")
        ]
        resolved_lockfile = ""
        if package_specs:
            try:
                resolved_lockfile = resolve_lockfile(package_specs)
            except (LockfileResolutionError, LockfileTooLargeError) as e:
                cleanup_repo(repo_dir)
                reason = f"Lockfile resolution failed: {e}"
                _record(
                    submission_id,
                    status="rejected",
                    error_message=reason,
                    completed_at=datetime.now(timezone.utc),
                )
                return {
                    "owner_repo": owner_repo,
                    "accepted": False,
                    "submission_id": submission_id,
                    "reason": reason,
                }

        _record(
            submission_id,
            resolved_lockfile=resolved_lockfile or None,
            status="ai_review" if llm_api_key else "container_test",
        )

        # repo_dir ownership transfers to the worker — it cleans up.
        job = SubmissionJob(
            submission_id=submission_id,
            repo_dir=repo_dir,
            manifest=manifest,
            llm_provider=llm_provider,
            llm_api_key=llm_api_key,
            author_github=author_github,
            repo_url=repo_url,
        )
        await validation_queue.enqueue(job)

        return {
            "owner_repo": owner_repo,
            "accepted": True,
            "submission_id": submission_id,
            "command_name": command_name,
            "version": version,
        }

    except RepoValidationError as e:
        if repo_dir:
            cleanup_repo(repo_dir)
        _record(
            submission_id,
            status="rejected",
            error_message=str(e),
            completed_at=datetime.now(timezone.utc),
        )
        return {
            "owner_repo": owner_repo,
            "accepted": False,
            "submission_id": submission_id,
            "reason": str(e),
        }
    except Exception as e:
        logger.exception("Bulk: failed to process %s", owner_repo)
        if repo_dir:
            cleanup_repo(repo_dir)
        _record(
            submission_id,
            status="rejected",
            error_message=f"Bulk processing error: {e}",
            completed_at=datetime.now(timezone.utc),
        )
        return {
            "owner_repo": owner_repo,
            "accepted": False,
            "submission_id": submission_id,
            "reason": str(e),
        }


# ── Batch status ─────────────────────────────────────────────────────────


@router.get("/v1/admin/bulk-submissions/{batch_id}")
def bulk_status(
    batch_id: str,
    x_admin_key: str = Header(..., alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Return per-repo status rows for every submission in a bulk batch."""
    _require_admin_key(x_admin_key)

    # Defer import — _build_stages lives in submit.py and we want to avoid
    # circular module load order at startup.
    from .submit import _build_stages

    subs = (
        db.query(Submission)
        .filter(Submission.batch_id == batch_id)
        .order_by(Submission.id)
        .all()
    )
    if not subs:
        raise HTTPException(404, "Batch not found")

    rows = []
    for s in subs:
        command_name = None
        if s.command_id:
            cmd = db.query(Command).filter(Command.id == s.command_id).first()
            if cmd:
                command_name = cmd.command_name
        elif s.static_analysis_result and isinstance(s.static_analysis_result, dict):
            command_name = s.static_analysis_result.get("command_name")
        rows.append({
            "submission_id": s.id,
            "github_repo_url": s.github_repo_url,
            "status": s.status,
            "command_name": command_name,
            "stages": _build_stages(s),
            "error_message": s.error_message,
            "external_run_url": s.external_run_url,
            "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        })

    by_status: dict[str, int] = {}
    for r in rows:
        key = r["status"]
        by_status[key] = by_status.get(key, 0) + 1

    return {
        "batch_id": batch_id,
        "total": len(rows),
        "by_status": by_status,
        "submissions": rows,
    }


# ── Operator GitHub OAuth ────────────────────────────────────────────────


class _OperatorCallbackRequest(BaseModel):
    code: str


@router.get("/v1/operator/auth/github")
def operator_github_authorize(redirect_uri: str = Query(...)):
    """Build the GitHub authorize URL for the operator OAuth app.

    Separate from /v1/auth/github so the operator page can use its own
    GitHub OAuth App (PANTRY_OPERATOR_GITHUB_CLIENT_ID/_SECRET) and
    register its own callback URL without colliding with the regular
    author-submission OAuth app.
    """
    settings = get_settings()
    if not settings.operator_github_client_id:
        raise HTTPException(503, "Operator GitHub OAuth not configured")
    url = (
        "https://github.com/login/oauth/authorize"
        f"?client_id={settings.operator_github_client_id}"
        f"&redirect_uri={redirect_uri}"
        "&scope=read:user,public_repo"
    )
    return {"url": url}


@router.post("/v1/operator/auth/github/callback")
async def operator_github_callback(body: _OperatorCallbackRequest):
    """Exchange an operator-app OAuth code for an access token.

    The returned token is held in browser sessionStorage and used to call
    api.github.com directly for the repo list. It is NOT passed to the
    bulk-submission endpoint — that's gated by X-Admin-Key only.
    """
    settings = get_settings()
    if not settings.operator_github_client_id or not settings.operator_github_client_secret:
        raise HTTPException(503, "Operator GitHub OAuth not configured")
    result = await exchange_github_code(
        body.code,
        client_id=settings.operator_github_client_id,
        client_secret=settings.operator_github_client_secret,
    )
    return {
        "access_token": result["access_token"],
        "user": {
            "github_id": result["github_id"],
            "github_username": result["github_username"],
            "display_name": result["display_name"],
            "avatar_url": result["avatar_url"],
        },
    }
