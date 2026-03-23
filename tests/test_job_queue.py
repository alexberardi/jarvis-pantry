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
