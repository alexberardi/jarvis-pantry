"""Submission pipeline — orchestrates clone, validate, AI review, publish.

Flow:
1. Validate GitHub auth (done by endpoint)
2. Clone repo
3. Validate structure + manifest
4. Run AI security review (using submitter's API key)
5. Publish or flag based on danger score
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Author, Command, CommandVersion, SecurityReport, Submission
from .github_service import (
    clone_repo,
    validate_structure,
    read_command_source,
    cleanup_repo,
    RepoValidationError,
)
from .security_review import run_security_review, SecurityReviewResult


async def process_submission(
    repo_url: str,
    llm_provider: str,
    llm_api_key: str,
    author: Author,
    db: Session,
) -> dict[str, Any]:
    """Process a command submission end-to-end.

    Args:
        repo_url: GitHub HTTPS URL of the command repo.
        llm_provider: "claude" or "openai".
        llm_api_key: Submitter's API key (used once, not stored).
        author: Authenticated Author record.
        db: Database session.

    Returns:
        Dict with submission result details.
    """
    # Create submission record
    submission = Submission(
        github_repo_url=repo_url,
        author_id=author.id,
        status="validating",
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    repo_dir = None
    try:
        # 1. Clone
        submission.status = "validating"
        db.commit()
        repo_dir = clone_repo(repo_url)

        # 2. Validate structure
        manifest = validate_structure(repo_dir)
        command_name = manifest["name"]
        version = manifest.get("version", "0.1.0")

        # Check uniqueness — if command exists, only original author can update
        existing = db.query(Command).filter(Command.command_name == command_name).first()
        if existing and existing.author_id != author.id:
            raise RepoValidationError(
                f"Command '{command_name}' is owned by a different author"
            )

        # 3. AI security review
        submission.status = "reviewing"
        db.commit()

        source_code = read_command_source(repo_dir)
        review: SecurityReviewResult = await run_security_review(
            source_code, llm_provider, llm_api_key
        )

        # 4. Create/update records
        if existing:
            command = existing
            command.description = manifest.get("description", command.description)
            command.display_name = manifest.get("display_name", command.display_name)
            command.github_repo_url = repo_url
            command.categories = manifest.get("categories", [])
            command.platforms = manifest.get("platforms", [])
            command.license = manifest.get("license")
            command.latest_version = version
            command.danger_rating = review.danger_score
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
                danger_rating=review.danger_score,
            )
            db.add(command)
            db.flush()

        # Security report
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
        cmd_version = CommandVersion(
            command_id=command.id,
            version=version,
            git_tag=f"v{version}",
            manifest_json=manifest,
            danger_rating=review.danger_score,
            security_report_id=report.id,
            min_jarvis_version=manifest.get("min_jarvis_version", "0.9.0"),
        )
        db.add(cmd_version)

        # 5. Publish or flag
        if review.danger_score <= 3 and review.recommendation != "reject":
            command.published = True
            submission.status = "published"
        else:
            command.published = False
            submission.status = "pending_review"

        submission.command_id = command.id
        submission.completed_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "submission_id": submission.id,
            "status": submission.status,
            "command_name": command_name,
            "version": version,
            "danger_rating": review.danger_score,
            "recommendation": review.recommendation,
            "summary": review.summary,
            "concerns": review.concerns,
            "published": command.published,
        }

    except RepoValidationError as e:
        submission.status = "rejected"
        submission.error_message = str(e)
        submission.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise

    except Exception as e:
        submission.status = "rejected"
        submission.error_message = str(e)
        submission.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise

    finally:
        if repo_dir:
            cleanup_repo(repo_dir)
