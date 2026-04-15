"""Shared publish/reject logic for the submission pipeline.

Both the in-process worker (LocalRunner) and the async callback endpoint
(GitHubActionsRunner) land here once a container-test result is known.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Author, Command, CommandVersion, SecurityReport, Submission
from .container_test import ContainerTestResult
from .security_review import SecurityReviewResult

logger = logging.getLogger(__name__)


_SKIP_SUMMARIES = {"SKIP - Docker not available", "SKIP - SDK not available"}


def _review_to_json(review: SecurityReviewResult | None) -> dict[str, Any] | None:
    if review is None:
        return None
    if is_dataclass(review):
        return asdict(review)
    return review  # type: ignore[return-value]


def _review_from_json(data: dict[str, Any] | None) -> SecurityReviewResult | None:
    if not data:
        return None
    return SecurityReviewResult(
        danger_score=data["danger_score"],
        summary=data["summary"],
        concerns=data.get("concerns", []),
        recommendation=data["recommendation"],
        raw_response=data.get("raw_response", {}),
    )


def build_dispatch_context(
    *,
    manifest: dict[str, Any],
    review: SecurityReviewResult | None,
    author_github: str,
    repo_url: str,
) -> dict[str, Any]:
    """Serialize everything the finalize step needs when the container test
    runs out-of-process (e.g. GitHub Actions) and finalization happens later
    via the callback endpoint."""
    return {
        "manifest": manifest,
        "review": _review_to_json(review),
        "author_github": author_github,
        "repo_url": repo_url,
    }


def finalize_submission(
    *,
    db: Session,
    submission: Submission,
    manifest: dict[str, Any],
    review: SecurityReviewResult | None,
    author_github: str,
    repo_url: str,
    container_result: ContainerTestResult,
) -> None:
    """Apply the container-test outcome: either publish or reject."""

    submission.container_test_result = container_result.to_dict()

    # Reject path — container tests failed and were not a known skip.
    if not container_result.passed and container_result.summary not in _SKIP_SUMMARIES:
        submission.status = "rejected"
        error_detail = container_result.summary
        if container_result.errors:
            error_detail += " | " + "; ".join(container_result.errors[:3])
        submission.error_message = f"Container tests failed: {error_detail}"
        submission.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("Submission %d rejected: %s", submission.id, error_detail)
        return

    # Publish path
    command_name = manifest["name"]
    version = manifest.get("version", "0.1.0")
    components = manifest.get("components", [])
    is_bundle = len(components) > 1 or (
        len(components) == 1 and components[0].get("type") != "command"
    ) or (
        len(components) == 1 and "/" in components[0].get("path", "")
    )
    package_type = "bundle" if is_bundle else "command"
    danger_rating = review.danger_score if review else None

    existing = db.query(Command).filter(Command.command_name == command_name).first()
    if existing:
        command = existing
        command.description = manifest.get("description", command.description)
        command.display_name = manifest.get("display_name", command.display_name)
        command.github_repo_url = repo_url
        command.categories = manifest.get("categories", [])
        command.platforms = manifest.get("platforms", [])
        command.license = manifest.get("license")
        command.latest_version = version
        command.package_type = package_type
        command.components = components if components else None
        if danger_rating is not None:
            command.danger_rating = danger_rating
    else:
        author = db.query(Author).filter(Author.github_username == author_github).first()
        if not author:
            author = Author(
                github_id=0,
                github_username=author_github,
                display_name=author_github,
            )
            db.add(author)
            db.flush()

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
            danger_rating=danger_rating,
            package_type=package_type,
            components=components if components else None,
        )
        db.add(command)
        db.flush()

    report_id = None
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
        report_id = report.id

    existing_ver = db.query(CommandVersion).filter(
        CommandVersion.command_id == command.id,
        CommandVersion.version == version,
    ).first()
    if not existing_ver:
        db.add(CommandVersion(
            command_id=command.id,
            version=version,
            git_tag=None,
            manifest_json=manifest,
            danger_rating=danger_rating,
            security_report_id=report_id,
            min_jarvis_version=manifest.get("min_jarvis_version", "0.9.0"),
        ))

    command.published = True

    submission.command_id = command.id
    submission.status = "published"
    submission.completed_at = datetime.now(timezone.utc)
    submission.callback_token = None  # single-use, consume after finalize
    db.commit()
    logger.info("Submission %d published as %s", submission.id, command_name)
