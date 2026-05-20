"""Tests for the callback-timeout retry watcher (#22).

Per the engineering breakdown and QA test plan: a background loop scans
`awaiting_container` Submission rows, retries those stalled past
10/30/30-minute thresholds, and marks the row `callback_timeout` once 3
total attempts have elapsed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Author, Submission
from app.services.callback_timeout_watcher import (
    LOOP_INTERVAL_SECONDS,
    _scan_and_decide,
    callback_timeout_watcher,
)
from app.services.container_runner import GitHubActionsRunner, RunnerDispatch


FIXED_NOW = datetime(2026, 5, 18, 18, 0, 0, tzinfo=timezone.utc)

# Sentinel so tests can explicitly pass dispatch_context=None to seed a row
# with no context (the "corruption" case). Without this, the helper's defaulting
# treats None as "use default" and substitutes a valid dict.
_UNSET = object()


@pytest.fixture
def db_session(tmp_path):
    """SQLite session for watcher tests (separate from API tests)."""
    db_path = tmp_path / "watcher.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    # Seed an author so submission FK constraints hold.
    session.add(Author(id=1, github_id=42, github_username="alice"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_submission(
    session,
    *,
    submission_id: int = 99,
    status: str = "awaiting_container",
    awaiting_container_since: datetime | None = None,
    dispatch_attempts: int = 1,
    dispatch_context=_UNSET,
    callback_nonce: str = "old-token",
    external_run_url: str = "https://github.com/old/run",
    resolved_lockfile: str | None = None,
) -> Submission:
    if dispatch_context is _UNSET:
        dispatch_context = {
            "manifest": {"name": "x", "version": "1.0.0"},
            "repo_url": "https://github.com/test/repo",
            "author_github": "alice",
            "review": None,
        }
    sub = Submission(
        id=submission_id,
        github_repo_url="https://github.com/test/repo",
        author_id=1,
        status=status,
        awaiting_container_since=awaiting_container_since,
        dispatch_attempts=dispatch_attempts,
        dispatch_context=dispatch_context,
        callback_nonce=callback_nonce,
        external_run_url=external_run_url,
        resolved_lockfile=resolved_lockfile,
    )
    session.add(sub)
    session.commit()
    return sub


def _ga_runner_mock(dispatch_result=None, side_effect=None):
    """Build a GitHubActionsRunner-spec mock with an AsyncMock .dispatch."""
    runner = MagicMock(spec=GitHubActionsRunner)
    if side_effect is not None:
        runner.dispatch = AsyncMock(side_effect=side_effect)
    else:
        runner.dispatch = AsyncMock(
            return_value=dispatch_result or RunnerDispatch(
                result=None,
                external_run_url="https://github.com/new/run",
                callback_nonce="new-token",
            ),
        )
    return runner


class TestWatcherDecisions:
    """Single-row decisions at each threshold."""

    @pytest.mark.asyncio
    async def test_attempt_1_past_10min_retries(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=11),
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 1
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "awaiting_container"
        assert sub.dispatch_attempts == 2
        assert sub.awaiting_container_since == FIXED_NOW  # restamped
        assert sub.callback_nonce == "new-token"
        assert sub.external_run_url == "https://github.com/new/run"

    @pytest.mark.asyncio
    async def test_attempt_2_past_30min_retries(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=31),
            dispatch_attempts=2,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 1
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "awaiting_container"
        assert sub.dispatch_attempts == 3

    @pytest.mark.asyncio
    async def test_attempt_3_past_30min_marks_callback_timeout(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=31),
            dispatch_attempts=3,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "callback_timeout"
        assert sub.dispatch_attempts == 3
        assert sub.completed_at == FIXED_NOW
        assert sub.error_message is not None
        assert "timeout" in sub.error_message.lower()

    @pytest.mark.asyncio
    async def test_healthy_in_progress_row_untouched(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=5),
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.dispatch_attempts == 1
        assert sub.status == "awaiting_container"

    @pytest.mark.asyncio
    async def test_freshly_dispatched_attempt_3_row_untouched(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=15),
            dispatch_attempts=3,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "awaiting_container"
        assert sub.dispatch_attempts == 3
        assert runner.dispatch.call_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["published", "rejected", "callback_timeout"])
    async def test_non_awaiting_status_ignored(self, db_session, status):
        _seed_submission(
            db_session,
            status=status,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=60),
            dispatch_attempts=3,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == status  # unchanged

    @pytest.mark.asyncio
    async def test_attempt_1_exactly_10min_boundary_retries(self, db_session):
        # Watcher uses >= semantics: exactly-10-min row triggers retry.
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=10),
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_attempt_3_exactly_30min_boundary_marks_timeout(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=30),
            dispatch_attempts=3,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "callback_timeout"

    @pytest.mark.asyncio
    async def test_null_awaiting_container_since_is_skipped_safely(self, db_session, caplog):
        # Pre-migration in-flight row: status=awaiting_container, stamp=NULL.
        _seed_submission(
            db_session,
            awaiting_container_since=None,
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock()
        with caplog.at_level(logging.WARNING, logger="app.services.callback_timeout_watcher"), \
             patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "awaiting_container"
        # Logged a warning so an operator can spot the inconsistency.
        assert any("99" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_dispatch_attempts_zero_is_skipped_as_malformed(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=60),
            dispatch_attempts=0,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "awaiting_container"
        assert sub.dispatch_attempts == 0

    @pytest.mark.asyncio
    async def test_dispatch_context_missing_for_retry_falls_back_gracefully(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=11),
            dispatch_attempts=1,
            dispatch_context=None,  # corruption case
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        assert runner.dispatch.call_count == 0
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        assert sub.status == "callback_timeout"
        assert "dispatch_context" in (sub.error_message or "")

    @pytest.mark.asyncio
    async def test_dispatch_runtimeerror_leaves_row_for_next_tick(self, db_session):
        _seed_submission(
            db_session,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=11),
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock(side_effect=RuntimeError("workflow_dispatch failed: 503"))
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            # Must not propagate.
            await _scan_and_decide(db_session)
        sub = db_session.query(Submission).filter(Submission.id == 99).first()
        # Status preserved (not callback_timeout), attempts not bumped (bump-after-success).
        assert sub.status == "awaiting_container"
        assert sub.dispatch_attempts == 1

    @pytest.mark.asyncio
    async def test_unhandled_exception_in_tick_logs_and_returns(self, caplog):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("db connection lost")
        with caplog.at_level(logging.WARNING, logger="app.services.callback_timeout_watcher"):
            # Must not raise.
            await _scan_and_decide(broken_db)
        # And it logged something useful.
        assert any(
            "db connection lost" in (rec.message + str(getattr(rec, "exc_info", "") or ""))
            or "scan" in rec.message.lower()
            for rec in caplog.records
        )


class TestWatcherSweep:
    """Multi-row sweeps within a single tick."""

    @pytest.mark.asyncio
    async def test_multiple_eligible_rows_processed_in_one_tick(self, db_session):
        # Row 1: retry candidate (attempt-1-past-10min)
        _seed_submission(
            db_session,
            submission_id=101,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=15),
            dispatch_attempts=1,
        )
        # Row 2: fail candidate (attempt-3-past-30min)
        _seed_submission(
            db_session,
            submission_id=102,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=45),
            dispatch_attempts=3,
        )
        # Row 3: healthy
        _seed_submission(
            db_session,
            submission_id=103,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=5),
            dispatch_attempts=2,
        )
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        # Exactly one re-dispatch (for the retry candidate).
        assert runner.dispatch.call_count == 1
        r1 = db_session.query(Submission).filter(Submission.id == 101).first()
        r2 = db_session.query(Submission).filter(Submission.id == 102).first()
        r3 = db_session.query(Submission).filter(Submission.id == 103).first()
        assert r1.dispatch_attempts == 2
        assert r1.status == "awaiting_container"
        assert r2.status == "callback_timeout"
        assert r3.status == "awaiting_container"
        assert r3.dispatch_attempts == 2

    @pytest.mark.asyncio
    async def test_dispatch_failure_does_not_block_other_rows(self, db_session):
        _seed_submission(
            db_session,
            submission_id=201,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=15),
            dispatch_attempts=1,
        )
        _seed_submission(
            db_session,
            submission_id=202,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=15),
            dispatch_attempts=1,
        )
        # First call raises, second succeeds.
        runner = _ga_runner_mock(side_effect=[
            RuntimeError("transient GH 503"),
            RunnerDispatch(
                result=None,
                external_run_url="https://github.com/new/run",
                callback_nonce="new-token",
            ),
        ])
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        # Both rows attempted, no aborted-mid-tick.
        assert runner.dispatch.call_count == 2
        r1 = db_session.query(Submission).filter(Submission.id == 201).first()
        r2 = db_session.query(Submission).filter(Submission.id == 202).first()
        # Bump-after-success: failure leaves attempts unchanged.
        assert r1.dispatch_attempts == 1
        assert r1.status == "awaiting_container"
        assert r2.dispatch_attempts == 2
        assert r2.status == "awaiting_container"

    @pytest.mark.asyncio
    async def test_empty_db_no_eligible_rows(self, db_session):
        runner = _ga_runner_mock()
        with patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)  # no-op
        assert runner.dispatch.call_count == 0


class TestWatcherLoop:
    """The outer background loop survives single-tick failures."""

    @pytest.mark.asyncio
    async def test_loop_continues_after_exception_in_tick(self):
        call_count = 0

        async def fake_tick(db):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated tick failure")
            if call_count >= 2:
                raise asyncio.CancelledError

        # Make the loop's sleep a no-op and SessionLocal a harmless mock.
        with patch("app.services.callback_timeout_watcher._scan_and_decide", new=fake_tick), \
             patch("app.services.callback_timeout_watcher.SessionLocal", new=MagicMock()), \
             patch("app.services.callback_timeout_watcher.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await callback_timeout_watcher()
        assert call_count >= 2


class TestWatcherLogging:
    """Every decision emits a structured log line."""

    @pytest.mark.asyncio
    async def test_every_decision_emits_structured_log(self, db_session, caplog):
        _seed_submission(
            db_session,
            submission_id=301,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=15),
            dispatch_attempts=1,
        )
        _seed_submission(
            db_session,
            submission_id=302,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=45),
            dispatch_attempts=3,
        )
        _seed_submission(
            db_session,
            submission_id=303,
            awaiting_container_since=FIXED_NOW - timedelta(minutes=5),
            dispatch_attempts=1,
        )
        runner = _ga_runner_mock()
        with caplog.at_level(logging.INFO, logger="app.services.callback_timeout_watcher"), \
             patch("app.services.callback_timeout_watcher._now", return_value=FIXED_NOW), \
             patch("app.services.callback_timeout_watcher.get_runner", return_value=runner):
            await _scan_and_decide(db_session)
        text = "\n".join(rec.message for rec in caplog.records)
        # Each submission appears in the logs with its decision.
        assert "301" in text
        assert "302" in text
        assert "303" in text


class TestLoopInterval:
    """The loop interval is reasonable for the deployment cadence (60s)."""

    def test_loop_interval_is_60_seconds(self):
        # Per breakdown: "loop every 60s". Locking this in so future edits
        # don't silently move it to e.g. 5s (busy-spin) or 600s (slow recovery).
        assert LOOP_INTERVAL_SECONDS == 60
