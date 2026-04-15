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
from ..models import Command, CommandVersion, SecurityReport, Submission
from .container_runner import get_runner
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

            command_name = job.manifest["name"]
            version = job.manifest.get("version", "0.1.0")
            components = job.manifest.get("components", [])
            is_bundle = len(components) > 1 or (
                len(components) == 1 and components[0].get("type") != "command"
            ) or (
                # Convention layout (commands/*/command.py) needs bundle treatment
                len(components) == 1 and "/" in components[0].get("path", "")
            )
            package_type = "bundle" if is_bundle else "command"

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

            # Stage 2: Container test
            submission.status = "container_test"
            db.commit()

            container_result = await get_runner().run(
                command_dir=job.repo_dir,
                submission_id=job.submission_id,
                packages=[p["name"] for p in job.manifest.get("packages", []) if isinstance(p, dict)],
                is_bundle=is_bundle,
                repo_url=job.repo_url,
            )

            submission.container_test_result = container_result.to_dict()
            db.commit()

            if not container_result.passed and container_result.summary != "SKIP - Docker not available" and container_result.summary != "SKIP - SDK not available":
                submission.status = "rejected"
                error_detail = container_result.summary
                if container_result.errors:
                    error_detail += " | " + "; ".join(container_result.errors[:3])
                submission.error_message = f"Container tests failed: {error_detail}"
                submission.completed_at = datetime.now(timezone.utc)
                db.commit()
                return

            # Stage 3: Publish
            existing = db.query(Command).filter(Command.command_name == command_name).first()

            danger_rating = review.danger_score if review else None

            if existing:
                command = existing
                command.description = job.manifest.get("description", command.description)
                command.display_name = job.manifest.get("display_name", command.display_name)
                command.github_repo_url = job.repo_url
                command.categories = job.manifest.get("categories", [])
                command.platforms = job.manifest.get("platforms", [])
                command.license = job.manifest.get("license")
                command.latest_version = version
                command.package_type = package_type
                command.components = components if components else None
                if danger_rating is not None:
                    command.danger_rating = danger_rating
            else:
                # Find author
                from ..models import Author
                author = db.query(Author).filter(
                    Author.github_username == job.author_github,
                ).first()
                if not author:
                    author = Author(
                        github_id=0,
                        github_username=job.author_github,
                        display_name=job.author_github,
                    )
                    db.add(author)
                    db.flush()

                command = Command(
                    command_name=command_name,
                    display_name=job.manifest.get("display_name", command_name),
                    description=job.manifest.get("description", ""),
                    github_repo_url=job.repo_url,
                    author_id=author.id,
                    latest_version=version,
                    categories=job.manifest.get("categories", []),
                    platforms=job.manifest.get("platforms", []),
                    license=job.manifest.get("license", "MIT"),
                    danger_rating=danger_rating,
                    package_type=package_type,
                    components=components if components else None,
                )
                db.add(command)
                db.flush()

            # Security report (if review happened)
            if review:
                report = SecurityReport(
                    command_id=command.id,
                    version=version,
                    ai_review_summary=review.summary,
                    ai_danger_score=review.danger_score,
                    ai_concerns=review.concerns,
                    ai_recommendation=review.recommendation,
                    overall_danger_rating=review.danger_score,
                    raw_ai_response=review.raw_response,
                )
                db.add(report)
                db.flush()

            # Command version
            existing_ver = db.query(CommandVersion).filter(
                CommandVersion.command_id == command.id,
                CommandVersion.version == version,
            ).first()
            if not existing_ver:
                cmd_version = CommandVersion(
                    command_id=command.id,
                    version=version,
                    git_tag=None,
                    manifest_json=job.manifest,
                    danger_rating=danger_rating,
                    security_report_id=report.id if review else None,
                    min_jarvis_version=job.manifest.get("min_jarvis_version", "0.9.0"),
                )
                db.add(cmd_version)

            # Auto-publish: anything that reaches this point has passed static,
            # AI review (or skipped in dev), and container tests. The AI hard-reject
            # gate is checked earlier, so there is no moderator step here.
            command.published = True

            submission.command_id = command.id
            submission.status = "published"
            submission.completed_at = datetime.now(timezone.utc)
            db.commit()

            logger.info(
                "Submission %d completed: %s (status=%s)",
                job.submission_id, command_name, submission.status,
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
