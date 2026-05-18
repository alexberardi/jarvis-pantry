"""Command submission and submission status endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import validate_github_token
from ..config import get_settings
from ..db import get_db
from ..models import Author, Command, CommandVersion, Submission
from ..rate_limiter import RateLimiter
from ..services.github_service import (
    RepoValidationError,
    clone_repo,
    cleanup_repo,
    read_component_sources,
    validate_structure,
    verify_repo_access,
)
from ..services.job_queue import SubmissionJob, validation_queue
from ..services.lockfile_resolver import (
    LockfileResolutionError,
    LockfileTooLargeError,
    resolve_lockfile,
)
from ..services import rejection_codes as rc
from ..services.rejection_codes import Finding, doc_url, make_finding
from ..services.security_review import estimate_review_cost
from ..services.static_analysis import run_static_analysis
from ..services.submission_pipeline import process_submission

router = APIRouter()

# Rate limiter for quick-submit: 10/hour per IP
_submit_limiter = RateLimiter(requests_per_hour=get_settings().submission_rate_limit_per_hour)

# Semaphore to limit concurrent git clones
_clone_semaphore = asyncio.Semaphore(get_settings().max_concurrent_clones)


# ── Full submission (GitHub OAuth + AI review) ──────────────────────────


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
    if body.llm_provider not in ("claude", "openai"):
        raise HTTPException(400, "llm_provider must be 'claude' or 'openai'")

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


# ── Submission status (GitHub OAuth) ─────────────────────────────────────


@router.get("/v1/submissions/{submission_id}")
def get_submission(
    submission_id: int,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Check status of a submission (requires GitHub auth)."""
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


# ── Public submission status (for polling) ──────────────────────────────


@router.get("/v1/submissions/{submission_id}/status")
def get_submission_status(
    submission_id: int,
    db: Session = Depends(get_db),
):
    """Public status endpoint for polling submission progress.

    No auth required — submission_id acts as a nonce.
    """
    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission:
        raise HTTPException(404, "Submission not found")

    # Build stages response
    stages = _build_stages(submission)

    # Build result for terminal states
    result = None
    if submission.status == "published" and submission.command_id:
        command = db.query(Command).filter(Command.id == submission.command_id).first()
        if command:
            result = {
                "command_name": command.command_name,
                "display_name": command.display_name,
                "version": command.latest_version,
            }
    elif submission.status == "rejected":
        result = {
            "reason": submission.error_message or "Unknown",
        }

    # Extract command_name from static analysis or DB
    command_name = None
    if submission.command_id:
        command = db.query(Command).filter(Command.id == submission.command_id).first()
        if command:
            command_name = command.command_name
    elif submission.static_analysis_result and isinstance(submission.static_analysis_result, dict):
        command_name = submission.static_analysis_result.get("command_name")

    return {
        "submission_id": submission.id,
        "status": submission.status,
        "command_name": command_name,
        "stages": stages,
        "result": result,
        "external_run_url": submission.external_run_url,
    }


def _build_stages(submission: Submission) -> dict:
    """Build the pipeline stages object for the status response."""
    status = submission.status

    # Status progression: pending → static_analysis → ai_review → container_test →
    # (awaiting_container if async) → published/rejected
    STAGE_ORDER = [
        "pending", "static_analysis", "ai_review", "container_test",
        "awaiting_container", "published", "rejected",
    ]

    def _stage_status(stage_name: str) -> str:
        """Determine if a stage is done, in_progress, or pending."""
        if status == "rejected":
            # Find which stage we were in when rejected
            if stage_name == "static_analysis" and submission.static_analysis_result:
                sa = submission.static_analysis_result
                return "passed" if sa.get("passed") else "failed"
            if stage_name == "ai_review":
                if submission.container_test_result is not None:
                    return "passed"  # we got past AI review
                if submission.static_analysis_result and not submission.static_analysis_result.get("passed"):
                    return "pending"
                if submission.error_message and "AI review rejected" in submission.error_message:
                    return "failed"
                return "pending"
            if stage_name == "container_test":
                if submission.container_test_result:
                    ct = submission.container_test_result
                    return "passed" if ct.get("passed") else "failed"
                return "pending"
            return "pending"

        if status == "published" or status == "pending_review":
            if stage_name in ("static_analysis", "container_test"):
                return "passed"
            if stage_name == "ai_review":
                return "passed" if submission.llm_provider else "skipped"
            return "done"

        if status == "awaiting_container":
            if stage_name == "static_analysis":
                return "passed"
            if stage_name == "ai_review":
                return "passed" if submission.llm_provider else "skipped"
            if stage_name == "container_test":
                return "in_progress"
            return "pending"

        # In-progress states
        if status == stage_name:
            return "in_progress"

        # Check if we've passed this stage
        try:
            current_idx = STAGE_ORDER.index(status)
            stage_idx = STAGE_ORDER.index(stage_name)
            if stage_idx < current_idx:
                return "passed" if stage_name != "pending" else "done"
        except ValueError:
            pass

        return "pending"

    stages: dict = {
        "queue": {"status": "done" if status != "pending" else "waiting"},
    }

    # Static analysis — dual-shape: prefer the new #18 `findings`/`reason_codes`
    # if present, otherwise wrap legacy flat-string entries on the fly.
    sa_status = _stage_status("static_analysis")
    sa_stage: dict = {"status": sa_status}
    if submission.static_analysis_result:
        sa = submission.static_analysis_result
        sa_stage["checks_passed"] = sa.get("checks_passed", 0)
        normalized = _normalize_static_analysis_result(sa)
        sa_stage["findings"] = normalized["findings"]
        sa_stage["warnings"] = normalized["warnings"]
        sa_stage["reason_codes"] = normalized["reason_codes"]
    stages["static_analysis"] = sa_stage

    # AI review
    ai_status = _stage_status("ai_review")
    ai_stage: dict = {"status": ai_status}
    if submission.llm_provider:
        ai_stage["provider"] = submission.llm_provider
    stages["ai_review"] = ai_stage

    # Container test
    ct_status = _stage_status("container_test")
    ct_stage: dict = {"status": ct_status}
    if submission.container_test_result:
        ct = submission.container_test_result
        ct_stage["pass_count"] = ct.get("pass_count", 0)
        ct_stage["fail_count"] = ct.get("fail_count", 0)
        ct_stage["test_count"] = ct.get("test_count", 0)
        ct_stage["errors"] = ct.get("errors", [])
    stages["container_test"] = ct_stage

    return stages


def _normalize_static_analysis_result(sa: dict) -> dict:
    """Dual-shape reader for the stored ``static_analysis_result`` JSON column.

    Pre-#18 rows have ``errors``/``warnings``/``dangerous_patterns`` as flat
    string lists. Post-#18 rows additionally have ``findings`` (errors) +
    ``warnings_structured`` + ``reason_codes`` carrying the structured shape.

    Returns a normalized dict with ``findings``, ``warnings`` (both lists of
    Finding-shaped dicts) and ``reason_codes`` (deduplicated list of strings).
    Legacy flat-string rows are wrapped as ``legacy_unstructured`` findings.

    Must never raise — a malformed row should degrade to an empty/partial
    response, not 500.
    """
    if not isinstance(sa, dict):
        return {"findings": [], "warnings": [], "reason_codes": []}

    has_new_shape = "findings" in sa or "reason_codes" in sa

    findings_out: list[dict] = []
    warnings_out: list[dict] = []
    reason_codes_seen: list[str] = []

    def _add(items: object, severity: str, target: list[dict]) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and "reason_code" in item:
                # Already-structured finding — pass through verbatim
                target.append(item)
                rc_str = item.get("reason_code")
                if isinstance(rc_str, str) and rc_str not in reason_codes_seen:
                    reason_codes_seen.append(rc_str)
            elif isinstance(item, str):
                # Legacy flat-string entry — wrap as legacy_unstructured
                target.append({
                    "reason_code": rc.LEGACY_UNSTRUCTURED,
                    "severity": severity,
                    "doc_url": doc_url(rc.LEGACY_UNSTRUCTURED),
                    "message": item,
                })
                if rc.LEGACY_UNSTRUCTURED not in reason_codes_seen:
                    reason_codes_seen.append(rc.LEGACY_UNSTRUCTURED)

    if has_new_shape:
        # Prefer the structured fields.
        _add(sa.get("findings"), "error", findings_out)
        # Post-#18 rows put warning-severity findings under `warnings_structured`
        # (the top-level `warnings` key still carries legacy strings).
        _add(sa.get("warnings_structured"), "warning", warnings_out)
        passthrough_codes = sa.get("reason_codes")
        if isinstance(passthrough_codes, list):
            for code in passthrough_codes:
                if isinstance(code, str) and code not in reason_codes_seen:
                    reason_codes_seen.append(code)
    else:
        # Legacy-only row — wrap the flat-string lists.
        _add(sa.get("errors"), "error", findings_out)
        _add(sa.get("warnings"), "warning", warnings_out)
        _add(sa.get("dangerous_patterns"), "warning", warnings_out)

    return {
        "findings": findings_out,
        "warnings": warnings_out,
        "reason_codes": reason_codes_seen,
    }


# ── Quick submit (no OAuth, with validation pipeline) ───────────────────


class QuickSubmitRequest(BaseModel):
    repo_url: str
    llm_provider: str = "claude"  # "claude" or "openai"
    llm_api_key: str = ""
    confirm: bool = False  # Must be True to enqueue; False = dry run (validate + cost estimate)


@router.post("/v1/commands/quick-submit")
async def quick_submit(
    body: QuickSubmitRequest,
    request: Request,
    author: Author = Depends(validate_github_token),
    db: Session = Depends(get_db),
):
    """Submit a command by GitHub URL with validation pipeline.

    Requires GitHub OAuth. Verifies the authenticated user has push access
    to the repo before accepting the submission.

    Two-step flow:
    1. Submit with confirm=false (default): verify ownership, clone, validate,
       static analysis, return cost estimate. Repo is cleaned up.
    2. Submit again with confirm=true: same validation, then enqueues for
       AI review + container test.
    """
    settings = get_settings()

    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _submit_limiter.check(client_ip):
        raise HTTPException(429, "Rate limit exceeded. Try again later.")

    repo_url = body.repo_url.rstrip("/")
    if not repo_url.startswith("https://github.com/"):
        raise HTTPException(400, "repo_url must be a public GitHub HTTPS URL")

    # Enforce LLM key unless explicitly bypassed (dev mode)
    if not settings.bypass_llm_key and not body.llm_api_key:
        raise HTTPException(400, "llm_api_key is required. Provide your Claude or OpenAI API key for security review.")

    if body.llm_api_key and body.llm_provider not in ("claude", "openai"):
        raise HTTPException(400, "llm_provider must be 'claude' or 'openai'")

    # Verify repo ownership — extract token from the already-validated author
    # The raw token is in the Authorization header
    auth_header = request.headers.get("Authorization", "")
    github_token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    try:
        await verify_repo_access(repo_url, github_token)
    except RepoValidationError as e:
        raise HTTPException(403, str(e))

    repo_dir = None
    try:
        # 1. Clone (sync, with semaphore)
        async with _clone_semaphore:
            repo_dir = await asyncio.to_thread(clone_repo, repo_url)

        # 2. Validate structure
        manifest = validate_structure(repo_dir)
        command_name = manifest["name"]
        version = manifest.get("version", "0.1.0")

        # 3. Static analysis (sync, fast)
        analysis = run_static_analysis(repo_dir)

        if not analysis.passed:
            cleanup_repo(repo_dir)
            # Hard cut to the #18 structured rejection envelope. No `errors`,
            # no `dangerous_patterns`, no `errors_legacy` — per Alex's call on
            # the #18 thread (2026-05-18).
            error_findings = [f.to_dict() for f in analysis.findings if f.severity == "error"]
            warning_findings = [f.to_dict() for f in analysis.findings if f.severity == "warning"]
            raise HTTPException(422, detail={
                "result": "rejected",
                "message": "Static analysis failed",
                "reason_codes": analysis.reason_codes,
                "findings": error_findings,
                "warnings": warning_findings,
            })

        # 4. Estimate AI review cost (if key provided)
        cost_estimate = None
        if body.llm_api_key:
            comp_sources = read_component_sources(repo_dir, manifest)
            source_code = "\n\n".join(comp_sources.values())
            cost_estimate = estimate_review_cost(source_code, body.llm_provider)

        # 4b. Dry run — return validation + cost without enqueuing
        if not body.confirm:
            cleanup_repo(repo_dir)
            return {
                "status": "preview",
                "command_name": command_name,
                "version": version,
                "static_analysis": analysis.to_dict(),
                "cost_estimate": cost_estimate,
            }

        # 5. Per-user rate limit. Counts confirmed submissions in the last hour
        # from the submissions table; previews (confirm=false) are free. DB-backed
        # so it's consistent across Fly machines and survives restarts.
        if not settings.rate_limit_disabled:
            user_limit = settings.submission_rate_limit_per_user_per_hour
            recent_count = db.query(Submission).filter(
                Submission.author_id == author.id,
                Submission.submitted_at > datetime.now(timezone.utc) - timedelta(hours=1),
            ).count()
            if recent_count >= user_limit:
                cleanup_repo(repo_dir)
                raise HTTPException(
                    429,
                    f"Rate limit exceeded: {user_limit} submissions per hour per user. "
                    "Please try again later.",
                )

        # 5b. Resolve lockfile (synchronous at acceptance, per #21).
        # No submission row is created on failure — author sees the rejection
        # envelope and re-submits after fixing the manifest.
        package_specs = [
            p["name"] for p in manifest.get("packages", []) or []
            if isinstance(p, dict) and p.get("name")
        ]
        try:
            resolved_lockfile = resolve_lockfile(package_specs)
        except LockfileTooLargeError as e:
            cleanup_repo(repo_dir)
            finding = make_finding(
                rc.RESOLVED_LOCKFILE_EXCEEDS_SIZE_CAP, "error", message=str(e),
            )
            raise HTTPException(422, detail={
                "result": "rejected",
                "message": "Resolved lockfile exceeds size cap",
                "reason_codes": [rc.RESOLVED_LOCKFILE_EXCEEDS_SIZE_CAP],
                "findings": [finding.to_dict()],
                "warnings": [],
            })
        except LockfileResolutionError as e:
            cleanup_repo(repo_dir)
            finding = make_finding(
                rc.LOCKFILE_RESOLUTION_FAILED, "error", message=str(e),
            )
            raise HTTPException(422, detail={
                "result": "rejected",
                "message": f"Lockfile resolution failed: {e}",
                "reason_codes": [rc.LOCKFILE_RESOLUTION_FAILED],
                "findings": [finding.to_dict()],
                "warnings": [],
            })

        # 6. Create submission record
        submission = Submission(
            github_repo_url=repo_url,
            author_id=author.id,
            status="static_analysis",
            static_analysis_result=analysis.to_dict(),
            llm_provider=body.llm_provider if body.llm_api_key else None,
            resolved_lockfile=resolved_lockfile or None,
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)

        # 7. Enqueue async job (repo_dir ownership transfers to worker)
        job = SubmissionJob(
            submission_id=submission.id,
            repo_dir=repo_dir,
            manifest=manifest,
            llm_provider=body.llm_provider,
            llm_api_key=body.llm_api_key,
            author_github=author.github_username,
            repo_url=repo_url,
        )
        await validation_queue.enqueue(job)

        submission.status = "ai_review" if body.llm_api_key else "container_test"
        db.commit()

        return {
            "submission_id": submission.id,
            "status": "queued",
            "command_name": command_name,
            "version": version,
            "static_analysis": analysis.to_dict(),
            "cost_estimate": cost_estimate,
        }

    except HTTPException:
        raise
    except RepoValidationError as e:
        if repo_dir:
            cleanup_repo(repo_dir)
        raise HTTPException(422, str(e))
    except Exception as e:
        if repo_dir:
            cleanup_repo(repo_dir)
        raise HTTPException(500, f"Submission failed: {e}")


# ── Container test callback (for out-of-process runners) ────────────────


class ContainerResultCallback(BaseModel):
    passed: bool
    summary: str
    test_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    errors: list[str] = []
    raw_output: str = ""


@router.post("/v1/submissions/{submission_id}/container-result")
def container_result_callback(
    submission_id: int,
    body: ContainerResultCallback,
    request: Request,
    db: Session = Depends(get_db),
):
    """Callback from an out-of-process container test runner (e.g. GitHub Actions).

    Authenticated with a per-submission token issued when the runner was
    dispatched. The token is stored on the submission row and cleared after
    successful finalization — so it's single-use and scoped to one submission.
    """
    from ..services.container_test import ContainerTestResult
    from ..services.finalize import _review_from_json, finalize_submission

    submission = db.query(Submission).filter(Submission.id == submission_id).first()
    if not submission:
        raise HTTPException(404, "Submission not found")

    if submission.status != "awaiting_container":
        raise HTTPException(
            409,
            f"Submission is in status {submission.status!r}, not awaiting a container result",
        )

    expected_token = submission.callback_token
    provided_token = request.headers.get("X-Pantry-Token", "")
    if not expected_token or provided_token != expected_token:
        raise HTTPException(401, "Invalid or missing callback token")

    ctx = submission.dispatch_context or {}
    if not ctx:
        raise HTTPException(500, "Missing dispatch context — cannot finalize")

    container_result = ContainerTestResult(
        passed=body.passed,
        summary=body.summary,
        test_count=body.test_count,
        pass_count=body.pass_count,
        fail_count=body.fail_count,
        errors=body.errors,
        raw_output=body.raw_output,
    )

    finalize_submission(
        db=db,
        submission=submission,
        manifest=ctx["manifest"],
        review=_review_from_json(ctx.get("review")),
        author_github=ctx["author_github"],
        repo_url=ctx["repo_url"],
        container_result=container_result,
    )

    return {"status": submission.status, "submission_id": submission.id}
