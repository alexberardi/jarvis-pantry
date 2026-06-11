"""Async job queue for submission validation pipeline.

Uses asyncio.Queue with a bounded worker pool. Jobs are processed in order:
AI review → container test → publish/reject.

Pending jobs are lost on restart (submissions can be re-submitted).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Submission
from .container_runner import get_runner
from .finalize import build_dispatch_context, finalize_submission
from .github_service import cleanup_repo, read_component_sources
from .security_review import SecurityReviewResult, format_bundle_source, run_security_review

logger = logging.getLogger(__name__)


@dataclass
class SubmissionJob:
    """A queued submission job."""

    submission_id: int
    repo_dir: Path
    manifest: dict[str, Any]
    llm_provider: str
    llm_api_key: str
    author_github: str
    repo_url: str = ""
    git_commit_sha: str | None = None

    def zero_key(self) -> None:
        """Zero out the API key after use."""
        self.llm_api_key = ""


class ValidationQueue:
    """Async queue for processing submission validations."""

    def __init__(self, max_workers: int = 3):
        self._queue: asyncio.Queue[SubmissionJob] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max_workers)
        self._workers: list[asyncio.Task[None]] = []
        self._running = False

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()

    async def start(self, num_workers: int = 3) -> None:
        """Start worker tasks."""
        self._running = True
        for i in range(num_workers):
            task = asyncio.create_task(self._worker_loop(i))
            self._workers.append(task)
        logger.info("Validation queue started with %d workers", num_workers)

    async def stop(self) -> None:
        """Stop all workers gracefully."""
        self._running = False
        # Put sentinel values to unblock workers
        for _ in self._workers:
            await self._queue.put(None)  # type: ignore[arg-type]
        for task in self._workers:
            task.cancel()
        self._workers.clear()
        logger.info("Validation queue stopped")

    async def enqueue(self, job: SubmissionJob) -> None:
        """Add a job to the queue."""
        await self._queue.put(job)
        logger.info("Enqueued submission %d (queue size: %d)", job.submission_id, self._queue.qsize())

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker loop that processes jobs from the queue."""
        while self._running:
            try:
                job = await self._queue.get()
                if job is None:
                    break

                async with self._semaphore:
                    await self._process_job(job, worker_id)

                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Worker %d unhandled error: %s", worker_id, e)

    async def _process_job(self, job: SubmissionJob, worker_id: int) -> None:
        """Process a single submission job through the pipeline."""
        db: Session = SessionLocal()
        try:
            submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
            if not submission:
                logger.error("Submission %d not found", job.submission_id)
                return

            components = job.manifest.get("components", [])
            is_bundle = len(components) > 1 or (
                len(components) == 1 and components[0].get("type") != "command"
            ) or (
                # Convention layout (commands/*/command.py) needs bundle treatment
                len(components) == 1 and "/" in components[0].get("path", "")
            )
            # Stage 1: AI security review
            review: SecurityReviewResult | None = None
            if job.llm_api_key:
                submission.status = "ai_review"
                db.commit()

                try:
                    comp_sources = read_component_sources(job.repo_dir, job.manifest)
                    if components:
                        # Build type-annotated sources for the review prompt
                        comp_type_map = {c["name"]: c.get("type", "command") for c in components}
                        typed_sources = {
                            name: (comp_type_map.get(name, "command"), src)
                            for name, src in comp_sources.items()
                        }
                        source_code = format_bundle_source(typed_sources)
                    else:
                        source_code = list(comp_sources.values())[0] if comp_sources else ""
                    review = await run_security_review(
                        source_code, job.llm_provider, job.llm_api_key,
                    )
                except Exception as e:
                    logger.error("AI review failed for submission %d: %s", job.submission_id, e)
                    review = None
                finally:
                    job.zero_key()

                # Only a hard "reject" recommendation blocks a submission.
                # danger_score is informational and surfaced on the package detail page.
                if review and review.recommendation == "reject":
                    submission.status = "rejected"
                    submission.error_message = f"AI review rejected: {review.summary}"
                    submission.static_analysis_result = submission.static_analysis_result  # keep existing
                    submission.completed_at = datetime.now(timezone.utc)
                    db.commit()
                    return
            else:
                # No API key (dev mode) — skip AI review
                submission.status = "container_test"
                db.commit()

            # Stage 2: Container test — dispatch to configured runner.
            submission.status = "container_test"
            db.commit()

            dispatch = await get_runner().dispatch(
                command_dir=job.repo_dir,
                submission_id=job.submission_id,
                lockfile_content=submission.resolved_lockfile or "",
                is_bundle=is_bundle,
                repo_url=job.repo_url,
            )

            if dispatch.pending:
                # Out-of-process runner (e.g. GitHub Actions). Stash everything
                # finalize_submission needs and wait for the callback endpoint.
                submission.status = "awaiting_container"
                submission.external_run_url = dispatch.external_run_url
                submission.callback_nonce = dispatch.callback_nonce
                submission.dispatch_context = build_dispatch_context(
                    manifest=job.manifest,
                    review=review,
                    author_github=job.author_github,
                    repo_url=job.repo_url,
                    git_commit_sha=job.git_commit_sha,
                )
                submission.awaiting_container_since = datetime.now(timezone.utc)
                submission.dispatch_attempts = (submission.dispatch_attempts or 0) + 1
                db.commit()
                logger.info(
                    "Submission %d awaiting container callback (%s)",
                    job.submission_id, dispatch.external_run_url,
                )
                return

            # Synchronous runner — finalize now.
            assert dispatch.result is not None
            finalize_submission(
                db=db,
                submission=submission,
                manifest=job.manifest,
                review=review,
                author_github=job.author_github,
                repo_url=job.repo_url,
                container_result=dispatch.result,
                git_commit_sha=job.git_commit_sha,
            )

        except Exception as e:
            logger.error("Job processing failed for submission %d: %s", job.submission_id, e)
            try:
                submission = db.query(Submission).filter(Submission.id == job.submission_id).first()
                if submission:
                    submission.status = "rejected"
                    submission.error_message = str(e)
                    submission.completed_at = datetime.now(timezone.utc)
                    db.commit()
            except Exception:
                logger.error("Failed to update submission status for %d", job.submission_id)
        finally:
            db.close()
            cleanup_repo(job.repo_dir)


# Module singleton
validation_queue = ValidationQueue()
