"""Tests for the async job queue."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.job_queue import SubmissionJob, ValidationQueue


@pytest.fixture
def job() -> SubmissionJob:
    return SubmissionJob(
        submission_id=1,
        repo_dir=Path("/tmp/fake-repo"),
        manifest={"name": "test_cmd", "version": "1.0.0"},
        llm_provider="claude",
        llm_api_key="sk-test-key",
        author_github="testuser",
    )


class TestSubmissionJob:
    def test_zero_key(self, job):
        assert job.llm_api_key == "sk-test-key"
        job.zero_key()
        assert job.llm_api_key == ""


class TestValidationQueue:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        queue = ValidationQueue(max_workers=2)
        await queue.start(num_workers=2)
        assert queue._running is True
        assert len(queue._workers) == 2
        await queue.stop()
        assert queue._running is False

    @pytest.mark.asyncio
    async def test_enqueue_increments_count(self, job):
        queue = ValidationQueue(max_workers=1)
        # Don't start workers — just test enqueue
        await queue.enqueue(job)
        assert queue.pending_count == 1

    @pytest.mark.asyncio
    async def test_pending_count(self, job):
        queue = ValidationQueue(max_workers=1)
        assert queue.pending_count == 0
        await queue.enqueue(job)
        assert queue.pending_count == 1

    @pytest.mark.asyncio
    async def test_worker_processes_job(self, job):
        """Verify worker picks up and processes a job."""
        queue = ValidationQueue(max_workers=1)

        # Mock the process method to track calls
        processed = []
        original_process = queue._process_job

        async def mock_process(j, worker_id):
            processed.append(j.submission_id)

        queue._process_job = mock_process

        await queue.start(num_workers=1)
        await queue.enqueue(job)

        # Give worker time to process
        await asyncio.sleep(0.1)

        await queue.stop()
        assert 1 in processed


# ── Lockfile-content dispatch (#21) ─────────────────────────────────────


from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Author, Submission
from app.services.container_runner import RunnerDispatch
from app.services.container_test import ContainerTestResult


@pytest.fixture
def db_session_factory(tmp_path):
    """Patch SessionLocal to a sqlite tmp DB so job_queue._process_job can use it."""
    db_path = tmp_path / "queue.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _seed_author_and_submission(session, *, resolved_lockfile: str | None):
    author = Author(id=1, github_id=10, github_username="alice")
    session.add(author)
    submission = Submission(
        id=99,
        github_repo_url="https://github.com/test/repo",
        author_id=1,
        status="container_test",
        resolved_lockfile=resolved_lockfile,
    )
    session.add(submission)
    session.commit()


class TestLockfileDispatch:
    """The worker pulls ``submission.resolved_lockfile`` and passes it as
    ``lockfile_content`` to the runner, NOT the legacy ``packages`` kwarg."""

    @pytest.mark.asyncio
    async def test_worker_passes_resolved_lockfile_to_dispatch(
        self, db_session_factory, tmp_path,
    ):
        # Seed DB
        session = db_session_factory()
        _seed_author_and_submission(
            session, resolved_lockfile="requests==2.31.0 --hash=sha256:abc\n",
        )
        session.close()

        # Mock the runner — dispatch() should be invoked with lockfile_content
        dispatch_mock = AsyncMock(return_value=RunnerDispatch(
            result=ContainerTestResult(
                passed=True, summary="2/2", test_count=2, pass_count=2, fail_count=0,
            ),
        ))
        fake_runner = MagicMock()
        fake_runner.dispatch = dispatch_mock

        job = SubmissionJob(
            submission_id=99,
            repo_dir=tmp_path,
            manifest={"name": "test", "version": "1.0.0", "packages": [{"name": "requests"}]},
            llm_provider="claude",
            llm_api_key="",  # skip AI review
            author_github="alice",
            repo_url="https://github.com/test/repo",
        )

        with patch("app.services.job_queue.SessionLocal", db_session_factory), \
             patch("app.services.job_queue.get_runner", return_value=fake_runner), \
             patch("app.services.job_queue.finalize_submission") as mock_finalize, \
             patch("app.services.job_queue.cleanup_repo"):
            queue = ValidationQueue(max_workers=1)
            await queue._process_job(job, worker_id=0)

        assert dispatch_mock.call_count == 1
        _args, kwargs = dispatch_mock.call_args
        assert kwargs.get("lockfile_content") == "requests==2.31.0 --hash=sha256:abc\n"
        assert "packages" not in kwargs

    @pytest.mark.asyncio
    async def test_worker_passes_empty_lockfile_when_none_stored(
        self, db_session_factory, tmp_path,
    ):
        session = db_session_factory()
        _seed_author_and_submission(session, resolved_lockfile=None)
        session.close()

        dispatch_mock = AsyncMock(return_value=RunnerDispatch(
            result=ContainerTestResult(
                passed=True, summary="ok", test_count=0, pass_count=0, fail_count=0,
            ),
        ))
        fake_runner = MagicMock()
        fake_runner.dispatch = dispatch_mock

        job = SubmissionJob(
            submission_id=99,
            repo_dir=tmp_path,
            manifest={"name": "test", "version": "1.0.0"},  # no packages
            llm_provider="claude",
            llm_api_key="",
            author_github="alice",
            repo_url="https://github.com/test/repo",
        )

        with patch("app.services.job_queue.SessionLocal", db_session_factory), \
             patch("app.services.job_queue.get_runner", return_value=fake_runner), \
             patch("app.services.job_queue.finalize_submission"), \
             patch("app.services.job_queue.cleanup_repo"):
            queue = ValidationQueue(max_workers=1)
            await queue._process_job(job, worker_id=0)

        assert dispatch_mock.call_count == 1
        _args, kwargs = dispatch_mock.call_args
        # Empty-string lockfile (GHA inputs are strings — None would serialize awkwardly)
        assert kwargs.get("lockfile_content") == ""
        assert "packages" not in kwargs
