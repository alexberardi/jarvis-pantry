"""Background watcher that retries stalled callback dispatches (#22).

When `GitHubActionsRunner` dispatches a workflow that silently drops (runner
outage, transient API hiccup, payload-shape regression), the `Submission` row
sits in `awaiting_container` forever. This watcher catches that case:

  - Stamps `awaiting_container_since` + bumps `dispatch_attempts` on each
    dispatch (done at the dispatch site in `job_queue._process_job`; the
    watcher's retry path bumps again on success).
  - Every 60s, scans `awaiting_container` rows. For each:
      * attempts=1 & age >= 10 min → re-dispatch (becomes attempts=2)
      * attempts=2 & age >= 30 min → re-dispatch (becomes attempts=3)
      * attempts=3 & age >= 30 min → mark `callback_timeout`
  - All decisions log a structured line (`watcher decision: …`).

The watcher tolerates pre-migration in-flight rows (NULL stamp), malformed
attempt counters, missing `dispatch_context`, and transient runner errors
without ever bubbling an exception out of the background task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Submission
from .container_runner import GitHubActionsRunner, get_runner

logger = logging.getLogger(__name__)


# Per #22 design (#19's "iii" backoff resolution): 10 min after attempt 1,
# 30 min after attempt 2, 30 min after attempt 3 before marking timeout.
THRESHOLDS_MINUTES: dict[int, int] = {1: 10, 2: 30, 3: 30}
MAX_ATTEMPTS = 3
LOOP_INTERVAL_SECONDS = 60


def _now() -> datetime:
    """Return current UTC time. Patched in tests to control the clock."""
    return datetime.now(timezone.utc)


def _threshold_for_attempt(attempts: int) -> Optional[timedelta]:
    mins = THRESHOLDS_MINUTES.get(attempts)
    return timedelta(minutes=mins) if mins is not None else None


def _is_bundle_from_manifest(manifest: dict) -> bool:
    components = manifest.get("components", [])
    return (
        len(components) > 1
        or (len(components) == 1 and components[0].get("type") != "command")
        or (len(components) == 1 and "/" in components[0].get("path", ""))
    )


async def _retry_submission(db: Session, submission: Submission) -> bool:
    """Re-dispatch a stalled submission. Returns True on success.

    Bump-after-success: `dispatch_attempts` is incremented only when
    `runner.dispatch(...)` returns normally, so a transient runner outage
    doesn't burn retry budget.
    """
    context = submission.dispatch_context or {}
    manifest = context.get("manifest")
    repo_url = context.get("repo_url")
    if not manifest or not repo_url:
        return False  # caller handles the missing-context failure path

    runner = get_runner()
    if not isinstance(runner, GitHubActionsRunner):
        # LocalRunner runs synchronously and never reaches awaiting_container,
        # so this path is defensive only. If somehow a LocalRunner row shows
        # up, skip rather than try a retry that would fail (the temp dir is
        # long gone by the time the watcher fires).
        logger.info(
            "watcher decision: submission_id=%d action=skipped_local_runner attempts=%d",
            submission.id, submission.dispatch_attempts or 0,
        )
        return True  # treat as "handled" so caller doesn't mark as missing-context

    dispatch = await runner.dispatch(
        command_dir=Path("/dev/null"),  # GHA runner only uses repo_url
        submission_id=submission.id,
        lockfile_content=submission.resolved_lockfile or "",
        is_bundle=_is_bundle_from_manifest(manifest),
        repo_url=repo_url,
    )
    submission.external_run_url = dispatch.external_run_url
    submission.callback_token = dispatch.callback_token
    submission.awaiting_container_since = _now()
    submission.dispatch_attempts = (submission.dispatch_attempts or 0) + 1
    db.commit()
    return True


async def _decide_for_submission(db: Session, submission: Submission, now: datetime) -> None:
    """One row's decision: healthy / retry / timeout / skip-malformed."""
    sub_id = submission.id
    attempts = submission.dispatch_attempts or 0

    if submission.awaiting_container_since is None:
        # Pre-migration in-flight row. Log a warning so an operator can
        # spot the inconsistency, but don't act — taking a corrective
        # action on a row with missing provenance would amplify upstream bugs.
        logger.warning(
            "watcher decision: submission_id=%d action=skipped_no_stamp attempts=%d",
            sub_id, attempts,
        )
        return

    if attempts < 1:
        # dispatch_attempts default is 0 — a stamped row with attempts=0
        # is internally inconsistent. Skip + log.
        logger.warning(
            "watcher decision: submission_id=%d action=skipped_malformed_attempts attempts=%d",
            sub_id, attempts,
        )
        return

    threshold = _threshold_for_attempt(attempts)
    if threshold is None:
        # attempts > MAX_ATTEMPTS — shouldn't happen, defensive.
        logger.warning(
            "watcher decision: submission_id=%d action=skipped_out_of_range attempts=%d",
            sub_id, attempts,
        )
        return

    age = now - submission.awaiting_container_since
    age_seconds = int(age.total_seconds())

    if age < threshold:
        logger.info(
            "watcher decision: submission_id=%d action=healthy attempts=%d age_seconds=%d",
            sub_id, attempts, age_seconds,
        )
        return

    # Past the threshold for the current attempt count.
    if attempts >= MAX_ATTEMPTS:
        submission.status = "callback_timeout"
        submission.error_message = (
            f"Container test timeout: no callback received after "
            f"{MAX_ATTEMPTS} dispatch attempts (final attempt {age_seconds // 60} min ago)"
        )
        submission.completed_at = now
        db.commit()
        logger.info(
            "watcher decision: submission_id=%d action=timed_out attempts=%d age_seconds=%d",
            sub_id, attempts, age_seconds,
        )
        return

    # Eligible to retry.
    try:
        ok = await _retry_submission(db, submission)
    except Exception as e:
        # Transient runner failure — leave in awaiting_container so the next
        # tick can retry. No attempts bump (bump-after-success above).
        db.rollback()
        logger.warning(
            "watcher decision: submission_id=%d action=dispatch_failed attempts=%d error=%s",
            sub_id, attempts, e,
        )
        return

    if ok:
        logger.info(
            "watcher decision: submission_id=%d action=retrying attempts=%d age_seconds=%d",
            sub_id, submission.dispatch_attempts or 0, age_seconds,
        )
    else:
        # dispatch_context is missing — can't retry without it. Fail-closed
        # so the row doesn't sit stuck forever.
        submission.status = "callback_timeout"
        submission.error_message = "retry skipped: dispatch_context missing or incomplete"
        submission.completed_at = now
        db.commit()
        logger.warning(
            "watcher decision: submission_id=%d action=missing_context_failed attempts=%d",
            sub_id, attempts,
        )


async def _scan_and_decide(db: Session) -> None:
    """One tick: examine all `awaiting_container` rows and act on stalled ones.

    Must never raise — the outer loop is a long-lived background task; an
    uncaught exception here would kill the loop and silently disable retry.
    """
    try:
        submissions = (
            db.query(Submission)
            .filter(Submission.status == "awaiting_container")
            .order_by(Submission.awaiting_container_since.asc().nulls_last())
            .all()
        )
    except Exception as e:
        logger.warning("watcher scan failed: %s", e)
        return

    now = _now()
    for submission in submissions:
        try:
            await _decide_for_submission(db, submission, now)
        except Exception as e:  # belt-and-suspenders — one bad row never blocks the rest
            db.rollback()
            logger.warning(
                "watcher decision: submission_id=%d action=unhandled_error error=%s",
                submission.id, e,
            )


async def callback_timeout_watcher() -> None:
    """Background loop: sweep stalled callbacks every LOOP_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)
        try:
            db = SessionLocal()
            try:
                await _scan_and_decide(db)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Callback timeout watcher error: %s", e)
