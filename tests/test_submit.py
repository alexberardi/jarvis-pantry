"""Tests for the submission endpoints."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
import yaml

from app.models import Author, Command, Submission
from app.services.github_service import RepoValidationError
from app.services.static_analysis import StaticAnalysisResult


class TestSubmitCommand:
    def test_invalid_provider(self, client, seed_data):
        """Reject non-claude/openai providers."""
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.post("/v1/commands", json={
            "repo_url": "https://github.com/test/repo",
            "llm_provider": "gemini",
            "llm_api_key": "key",
        })

        app.dependency_overrides.pop(validate_github_token, None)
        assert resp.status_code == 400

    def test_invalid_repo_url(self, client, seed_data):
        """Reject non-GitHub URLs."""
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.post("/v1/commands", json={
            "repo_url": "https://gitlab.com/test/repo",
            "llm_provider": "claude",
            "llm_api_key": "key",
        })

        app.dependency_overrides.pop(validate_github_token, None)
        assert resp.status_code == 400


class TestGetSubmission:
    def test_not_found(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.get("/v1/submissions/999")
        assert resp.status_code == 404

        app.dependency_overrides.pop(validate_github_token, None)

    def test_found(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        sub = Submission(
            id=1,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="published",
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get("/v1/submissions/1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

        app.dependency_overrides.pop(validate_github_token, None)


class TestGetSubmissionStatus:
    def test_not_found(self, client):
        resp = client.get("/v1/submissions/999/status")
        assert resp.status_code == 404

    def test_pending_submission(self, client, seed_data, db_session):
        sub = Submission(
            id=1,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="ai_review",
            llm_provider="claude",
            static_analysis_result={"passed": True, "checks_passed": 8, "warnings": [], "errors": [], "dangerous_patterns": []},
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get("/v1/submissions/1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ai_review"
        assert "stages" in data
        assert data["stages"]["static_analysis"]["status"] == "passed"

    def test_published_submission(self, client, seed_data, db_session):
        sub = Submission(
            id=1,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            command_id=seed_data["command"].id,
            status="published",
            static_analysis_result={"passed": True, "checks_passed": 8},
            container_test_result={"passed": True, "pass_count": 11, "fail_count": 0, "test_count": 11},
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get("/v1/submissions/1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["result"]["command_name"] == "get_stock_price"

    def test_callback_timeout_status(self, client, seed_data, db_session):
        """Rows in `callback_timeout` (#22) surface through the status endpoint
        with the error_message intact, peer of rejected."""
        sub = Submission(
            id=1,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="callback_timeout",
            error_message="Container test timed out: no callback received after 3 dispatch attempts",
            static_analysis_result={"passed": True, "checks_passed": 8},
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get("/v1/submissions/1/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "callback_timeout"
        # Reason text surfaces (peer of rejected handling).
        assert data["result"] is not None
        assert "timed out" in data["result"]["reason"]


def _make_fake_repo(tmp_path: Path) -> Path:
    """Create a valid command repo structure for testing."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    manifest = {
        "name": "test_quick",
        "description": "A quick test command",
        "version": "1.0.0",
        "display_name": "Quick Test",
        "categories": ["utilities"],
        "keywords": ["test"],
    }
    (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
    # Valid command.py with IJarvisCommand subclass
    (repo / "command.py").write_text("""\
from jarvis_command_sdk import IJarvisCommand

class TestQuick(IJarvisCommand):
    @property
    def command_name(self): return "test_quick"
    @property
    def description(self): return "A quick test command"
    @property
    def parameters(self): return []
    @property
    def required_secrets(self): return []
    @property
    def keywords(self): return ["test"]
    def run(self, ri, **kwargs): return {}
    def generate_prompt_examples(self): return []
    def generate_adapter_examples(self): return []
""")
    (repo / "README.md").write_text("# Test")
    (repo / "LICENSE").write_text("MIT")
    return repo


def _setup_quick_submit_auth(seed_data):
    """Override auth + repo access for quick-submit tests."""
    from app.main import app
    from app.auth import validate_github_token
    app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]


def _teardown_quick_submit_auth():
    from app.main import app
    from app.auth import validate_github_token
    app.dependency_overrides.pop(validate_github_token, None)


class TestQuickSubmit:
    def test_requires_auth(self, client):
        """Without auth, returns 422 (missing Authorization header)."""
        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/repo",
        })
        assert resp.status_code == 422

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    def test_requires_llm_key_by_default(self, mock_settings, mock_verify, client, seed_data):
        """When BYPASS_LLM_KEY=false (default), 400 if no key."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = False
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
        })
        assert resp.status_code == 400
        assert "llm_api_key" in resp.json()["detail"]
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_preview_dry_run(self, mock_clone, mock_settings, mock_verify, client, seed_data, db_session, tmp_path):
        """With confirm=false (default), returns preview without enqueuing."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "preview"
        assert data["command_name"] == "test_quick"
        assert data["static_analysis"]["passed"] is True
        assert "submission_id" not in data
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_confirm_enqueues(self, mock_queue, mock_clone, mock_settings, mock_verify, client, seed_data, db_session, tmp_path):
        """With confirm=true, enqueues for processing."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "submission_id" in data
        assert data["command_name"] == "test_quick"
        assert data["static_analysis"]["passed"] is True
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_cost_estimate_with_key(self, mock_clone, mock_settings, mock_verify, client, seed_data, db_session, tmp_path):
        """Preview with API key returns cost estimate."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "llm_provider": "claude",
            "llm_api_key": "sk-test-key",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "preview"
        assert data["cost_estimate"] is not None
        assert data["cost_estimate"]["estimated_cost_usd"] > 0
        assert data["cost_estimate"]["formatted"].startswith("~$")
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_missing_files(self, mock_clone, mock_settings, mock_verify, client, seed_data, tmp_path):
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        (repo / "command.py").write_text("# no manifest")
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/repo",
        })
        assert resp.status_code == 422
        assert "Missing" in resp.json()["detail"]
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_static_analysis_failure(self, mock_clone, mock_settings, mock_verify, client, seed_data, db_session, tmp_path):
        """Command with syntax error fails static analysis."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        manifest = {"name": "bad_cmd", "description": "Bad", "version": "1.0.0"}
        (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
        (repo / "command.py").write_text("def broken(:\n  pass")  # SyntaxError
        (repo / "README.md").write_text("# Test")
        (repo / "LICENSE").write_text("MIT")
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/bad-command",
        })
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "Static analysis failed" in detail["message"]
        # New envelope (#18) — findings list with structured items, no flat `errors` key
        assert any(
            "SyntaxError" in (f.get("message") or f.get("snippet") or "")
            for f in detail.get("findings", [])
        )
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_per_user_rate_limit(self, mock_queue, mock_clone, mock_settings, mock_verify, client, seed_data, db_session, tmp_path):
        """Per-user limit counts confirmed submissions in the last hour."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        # Seed the author to exactly the per-user limit.
        for _ in range(3):
            db_session.add(Submission(
                github_repo_url="https://github.com/test/prior",
                author_id=seed_data["author"].id,
                status="published",
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 429
        assert "per hour" in resp.json()["detail"]
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_quick_submit_honors_runtime_cap_change(
        self, mock_queue, mock_clone, mock_verify, client, seed_data, db_session, tmp_path, monkeypatch,
    ):
        """#31: mutating submission_rate_limit_per_hour mid-session changes the IP-cap
        threshold without a process restart. Without the live-cap fix, the 3rd request
        in this test would 429 because _submit_limiter was constructed with cap=2 at
        import time."""
        from app.api import submit as submit_mod

        _setup_quick_submit_auth(seed_data)
        # Reset shared IP-bucket state so prior tests in this run don't leak in.
        submit_mod._submit_limiter._buckets.clear()

        holder = {"cap": 2}
        monkeypatch.setattr(
            submit_mod,
            "get_settings",
            lambda: SimpleNamespace(
                bypass_llm_key=True,
                rate_limit_disabled=False,
                submission_rate_limit_per_hour=holder["cap"],
                submission_rate_limit_per_user_per_hour=100,
                max_concurrent_clones=5,
            ),
        )
        from app import config as config_mod
        monkeypatch.setattr(
            config_mod,
            "get_settings",
            lambda: SimpleNamespace(
                rate_limit_disabled=False,
                submission_rate_limit_per_hour=holder["cap"],
            ),
        )

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        # Two requests under cap=2 → both 200.
        for _ in range(2):
            resp = client.post("/v1/commands/quick-submit", json={
                "repo_url": "https://github.com/test/jarvis-command-test",
                "confirm": True,
            })
            assert resp.status_code == 200, resp.json()

        # Raise the cap mid-session; the 3rd request now fits.
        holder["cap"] = 100
        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200, resp.json()
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_per_user_rate_limit_bypassed_when_disabled(
        self, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """rate_limit_disabled=True is the dev escape hatch: submission accepted at the limit."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = True

        for _ in range(3):
            db_session.add(Submission(
                github_repo_url="https://github.com/test/prior",
                author_id=seed_data["author"].id,
                status="published",
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        assert "submission_id" in resp.json()
        _teardown_quick_submit_auth()

    @pytest.mark.parametrize("prior_count,expected_status", [
        (2, 200),   # under-limit: accepted
        (3, 429),   # at-limit (>=): rejected — locks the `>=` half of the comparator
        (10, 429),  # far-over: still rejected
    ])
    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_per_user_rate_limit_boundary(
        self, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
        prior_count, expected_status,
    ):
        """Boundary cases lock the `recent_count >= user_limit` comparator at submit.py:461."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        for _ in range(prior_count):
            db_session.add(Submission(
                github_repo_url="https://github.com/test/prior",
                author_id=seed_data["author"].id,
                status="published",
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == expected_status
        if expected_status == 429:
            assert "per hour" in resp.json()["detail"]
        else:
            assert "submission_id" in resp.json()
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_per_user_rate_limit_isolated_per_author(
        self, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """A different author's submissions must not count toward our limit."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        other_author = Author(
            id=999,
            github_id=99999,
            github_username="otheruser",
            display_name="Other User",
            avatar_url="https://example.com/other.png",
        )
        db_session.add(other_author)
        db_session.commit()

        # Seed 3 submissions for the OTHER author — should not count toward our limit
        for _ in range(3):
            db_session.add(Submission(
                github_repo_url="https://github.com/other/prior",
                author_id=other_author.id,
                status="published",
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        assert "submission_id" in resp.json()
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    def test_per_user_rate_limit_ignores_old_submissions(
        self, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """Submissions older than 1 hour are outside the rolling window — don't count."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        for _ in range(3):
            db_session.add(Submission(
                github_repo_url="https://github.com/test/prior",
                author_id=seed_data["author"].id,
                status="published",
                submitted_at=old_time,
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        assert "submission_id" in resp.json()
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_per_user_rate_limit_does_not_apply_to_previews(
        self, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """Previews (confirm=false) bypass the rate-limit check — it sits after the preview return."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 3
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        # Well over the limit
        for _ in range(5):
            db_session.add(Submission(
                github_repo_url="https://github.com/test/prior",
                author_id=seed_data["author"].id,
                status="published",
            ))
        db_session.commit()

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            # No confirm=True — preview mode
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "preview"
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, side_effect=RepoValidationError("You don't have push access"))
    @patch("app.api.submit.get_settings")
    def test_repo_access_denied(self, mock_settings, mock_verify, client, seed_data):
        """Submitting a repo you don't own returns 403."""
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        _setup_quick_submit_auth(seed_data)
        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/other/repo",
        })
        assert resp.status_code == 403
        assert "push access" in resp.json()["detail"]
        _teardown_quick_submit_auth()


class TestContainerResultCallback:
    """Callback endpoint used by out-of-process container test runners."""

    def _seed_awaiting(self, db_session, seed_data, *, token="tok-abc"):
        sub = Submission(
            github_repo_url="https://github.com/test/jarvis-command-widget",
            author_id=seed_data["author"].id,
            status="awaiting_container",
            llm_provider=None,
            callback_token=token,
            external_run_url="https://github.com/x/y/actions/runs/1",
            dispatch_context={
                "manifest": {
                    "name": "widget",
                    "display_name": "Widget",
                    "description": "Does widget things",
                    "version": "1.0.0",
                    "categories": ["utility"],
                    "platforms": ["linux"],
                    "license": "MIT",
                    "components": [{"name": "widget", "type": "command", "path": "command.py"}],
                },
                "review": None,
                "author_github": "testuser",
                "repo_url": "https://github.com/test/jarvis-command-widget",
            },
        )
        db_session.add(sub)
        db_session.commit()
        db_session.refresh(sub)
        return sub

    def test_publishes_on_pass(self, client, seed_data, db_session):
        sub = self._seed_awaiting(db_session, seed_data)

        resp = client.post(
            f"/v1/submissions/{sub.id}/container-result",
            headers={"X-Pantry-Token": "tok-abc"},
            json={"passed": True, "summary": "2/2 passed", "test_count": 2, "pass_count": 2},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

        db_session.expire_all()
        updated = db_session.query(Submission).filter(Submission.id == sub.id).first()
        assert updated is not None
        assert updated.status == "published"
        assert updated.callback_token is None  # single-use
        assert updated.command_id is not None

    def test_rejects_on_fail(self, client, seed_data, db_session):
        sub = self._seed_awaiting(db_session, seed_data)

        resp = client.post(
            f"/v1/submissions/{sub.id}/container-result",
            headers={"X-Pantry-Token": "tok-abc"},
            json={"passed": False, "summary": "1/2 failed", "test_count": 2, "pass_count": 1, "fail_count": 1, "errors": ["AssertionError: expected 3"]},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_rejects_bad_token(self, client, seed_data, db_session):
        sub = self._seed_awaiting(db_session, seed_data)

        resp = client.post(
            f"/v1/submissions/{sub.id}/container-result",
            headers={"X-Pantry-Token": "wrong"},
            json={"passed": True, "summary": "ok"},
        )
        assert resp.status_code == 401

    def test_wrong_status(self, client, seed_data, db_session):
        sub = Submission(
            github_repo_url="https://github.com/test/jarvis-command-widget",
            author_id=seed_data["author"].id,
            status="published",
            callback_token="tok-abc",
        )
        db_session.add(sub)
        db_session.commit()
        db_session.refresh(sub)

        resp = client.post(
            f"/v1/submissions/{sub.id}/container-result",
            headers={"X-Pantry-Token": "tok-abc"},
            json={"passed": True, "summary": "ok"},
        )
        assert resp.status_code == 409


# ── Structured rejection envelope (#18) ─────────────────────────────────


class TestRejectionEnvelopeShape:
    """422 response on static-analysis failure carries the new structured envelope.

    Per #18 — hard cut: no `errors_legacy`, no flat-string `errors: [string]`.
    Per Alex's resolution on #18 thread.
    """

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_static_analysis_failure_returns_new_envelope(
        self, mock_clone, mock_settings, mock_verify, client, seed_data, tmp_path
    ):
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        manifest = {"name": "bad_cmd", "description": "Bad", "version": "abc"}  # bad semver
        (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
        (repo / "command.py").write_text("def broken(:\n  pass")  # SyntaxError
        (repo / "README.md").write_text("# Test")
        (repo / "LICENSE").write_text("MIT")
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/bad-command",
        })
        assert resp.status_code == 422
        detail = resp.json()["detail"]

        # New envelope keys
        assert detail["result"] == "rejected"
        assert "reason_codes" in detail
        assert "findings" in detail
        assert "warnings" in detail
        assert "message" in detail

        # Hard cut — old keys are gone from the wire shape
        assert "errors" not in detail
        assert "dangerous_patterns" not in detail
        assert "errors_legacy" not in detail

        # findings is a list of dicts with reason_code + severity + doc_url
        assert isinstance(detail["findings"], list)
        for f in detail["findings"]:
            assert "reason_code" in f
            assert "severity" in f
            assert "doc_url" in f
            assert f["doc_url"].startswith("https://docs.jarvisautomation.dev/")

        # reason_codes deduplicates
        assert len(detail["reason_codes"]) == len(set(detail["reason_codes"]))

        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    def test_clean_submission_has_no_envelope_churn(
        self, mock_clone, mock_settings, mock_verify, client, seed_data, tmp_path
    ):
        """Regression: clean submission still returns 200 with the old preview shape."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "preview"
        assert "findings" not in data
        assert "reason_codes" not in data
        _teardown_quick_submit_auth()


class TestSubmissionStatusDualShape:
    """The /status reader handles both new-shape and legacy-shape stored JSON.

    Per Alex's resolution on #18: no retro re-running. Old rows keep their shape;
    the reader normalizes them on read.
    """

    def test_new_shape_row_serves_through_status_endpoint(self, client, seed_data, db_session):
        sub = Submission(
            id=10,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="ai_review",
            llm_provider="claude",
            static_analysis_result={
                "passed": True,
                "findings": [],
                "warnings": [],
                "dangerous_patterns": [],
                "errors": [],
                "reason_codes": [],
                "checks_passed": 8,
            },
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f"/v1/submissions/{sub.id}/status")
        assert resp.status_code == 200
        sa = resp.json()["stages"]["static_analysis"]
        assert sa["status"] == "passed"
        assert sa["findings"] == []
        assert sa["reason_codes"] == []

    def test_legacy_shape_row_wraps_into_new_envelope(self, client, seed_data, db_session):
        """A pre-cutover row stored with flat strings wraps to legacy_unstructured findings."""
        sub = Submission(
            id=11,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="rejected",
            static_analysis_result={
                "passed": False,
                "errors": ["Component 'x': missing required method/property: run"],
                "warnings": ["Unknown category: foo"],
                "dangerous_patterns": ["Dangerous call: eval()"],
                "checks_passed": 4,
            },
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f"/v1/submissions/{sub.id}/status")
        assert resp.status_code == 200
        sa = resp.json()["stages"]["static_analysis"]

        # Wrapped legacy strings appear as findings with reason_code=legacy_unstructured
        all_findings = sa.get("findings", []) + sa.get("warnings", [])
        legacy_findings = [f for f in all_findings if isinstance(f, dict) and f.get("reason_code") == "legacy_unstructured"]
        assert len(legacy_findings) >= 1
        # Each carries the original string in `message`
        messages = [f.get("message", "") for f in legacy_findings]
        assert any("missing required method" in m for m in messages)

    def test_legacy_row_with_no_failures_serves_clean(self, client, seed_data, db_session):
        sub = Submission(
            id=12,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="published",
            static_analysis_result={
                "passed": True,
                "errors": [],
                "warnings": [],
                "dangerous_patterns": [],
                "checks_passed": 8,
            },
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f"/v1/submissions/{sub.id}/status")
        assert resp.status_code == 200
        sa = resp.json()["stages"]["static_analysis"]
        assert sa.get("findings", []) == []
        assert sa.get("warnings", []) == []

    def test_null_static_analysis_result_serves_clean(self, client, seed_data, db_session):
        sub = Submission(
            id=13,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="pending",
            static_analysis_result=None,
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f"/v1/submissions/{sub.id}/status")
        assert resp.status_code == 200
        # Endpoint doesn't 500; the static_analysis stage is still present
        assert "static_analysis" in resp.json()["stages"]

    def test_malformed_legacy_row_does_not_500(self, client, seed_data, db_session):
        sub = Submission(
            id=14,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="rejected",
            # Intentionally malformed — `errors` is a string, not a list
            static_analysis_result={"passed": False, "errors": "not a list", "warnings": [], "checks_passed": 0},
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f"/v1/submissions/{sub.id}/status")
        # Contract: must not 500
        assert resp.status_code == 200


# ── Lockfile resolution at submission acceptance (#21) ──────────────────


def _make_fake_repo_with_packages(tmp_path: Path, packages: list[dict]) -> Path:
    """Extends _make_fake_repo with a `packages:` field on the manifest."""
    repo = _make_fake_repo(tmp_path)
    manifest_path = repo / "jarvis_command.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["packages"] = packages
    manifest_path.write_text(yaml.dump(manifest))
    return repo


_HAPPY_LOCKFILE = (
    "requests==2.31.0 --hash=sha256:abc\n"
    "pyyaml==6.0 --hash=sha256:def\n"
)


def _ok_proc(stdout: str = _HAPPY_LOCKFILE, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["uv", "pip", "compile"], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail_proc(stderr: str = "ERROR: No matching distribution") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["uv", "pip", "compile"], returncode=1, stdout="", stderr=stderr,
    )


class TestLockfileResolution:
    """Synchronous resolver invocation on the quick-submit acceptance path.

    Resolver lives at app.services.lockfile_resolver.resolve_lockfile() —
    a thin wrapper around `uv pip compile`. Tests patch the wrapper, not
    the subprocess, so the contract is the helper's behavior (returns a
    string lockfile, raises on failure / oversize).
    """

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    @patch("app.api.submit.resolve_lockfile")
    def test_resolves_lockfile_for_manifest_with_packages(
        self, mock_resolve, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(
            tmp_path, [{"name": "requests"}, {"name": "pyyaml"}],
        )
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()
        mock_resolve.return_value = _HAPPY_LOCKFILE

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200

        # Submission row persists the resolved lockfile
        sub = db_session.query(Submission).order_by(Submission.id.desc()).first()
        assert sub is not None
        assert sub.resolved_lockfile == _HAPPY_LOCKFILE

        # Resolver was invoked exactly once with the package names
        assert mock_resolve.call_count == 1
        args, kwargs = mock_resolve.call_args
        called_with = args[0] if args else kwargs.get("packages")
        assert list(called_with) == ["requests", "pyyaml"]
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    @patch("app.api.submit.resolve_lockfile")
    def test_empty_packages_list_skips_resolver(
        self, mock_resolve, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [])
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        sub = db_session.query(Submission).order_by(Submission.id.desc()).first()
        # Stored lockfile is empty (or None) when no packages declared
        assert not sub.resolved_lockfile
        assert mock_resolve.call_count == 0
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    @patch("app.api.submit.resolve_lockfile")
    def test_no_packages_key_skips_resolver(
        self, mock_resolve, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo(tmp_path)  # no packages key
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        assert mock_resolve.call_count == 0
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.resolve_lockfile")
    def test_preview_dry_run_skips_resolver(
        self, mock_resolve, mock_clone, mock_settings, mock_verify,
        client, seed_data, tmp_path,
    ):
        """Preview path (confirm=false) does NOT invoke the resolver — resolution
        is part of acceptance, not preview."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [{"name": "requests"}])
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            # confirm omitted → default False (dry run)
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "preview"
        assert mock_resolve.call_count == 0
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    @patch("app.api.submit.resolve_lockfile")
    def test_lockfile_exactly_at_cap_passes(
        self, mock_resolve, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """A resolved lockfile of exactly 50KB (51200 bytes) is accepted."""
        from app.services.lockfile_resolver import LOCKFILE_SIZE_CAP_BYTES
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [{"name": "requests"}])
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()
        mock_resolve.return_value = "a" * LOCKFILE_SIZE_CAP_BYTES

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        sub = db_session.query(Submission).order_by(Submission.id.desc()).first()
        assert sub is not None
        assert len(sub.resolved_lockfile) == LOCKFILE_SIZE_CAP_BYTES
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.resolve_lockfile")
    def test_lockfile_one_byte_over_cap_rejected(
        self, mock_resolve, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """One byte over the cap triggers a structured rejection."""
        from app.services.lockfile_resolver import (
            LOCKFILE_SIZE_CAP_BYTES,
            LockfileTooLargeError,
        )
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [{"name": "requests"}])
        mock_clone.return_value = repo
        oversize = "a" * (LOCKFILE_SIZE_CAP_BYTES + 1)
        mock_resolve.side_effect = LockfileTooLargeError(
            f"Resolved lockfile is {len(oversize)} bytes, exceeds {LOCKFILE_SIZE_CAP_BYTES}",
        )

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["result"] == "rejected"
        assert "resolved_lockfile_exceeds_size_cap" in detail["reason_codes"]
        # No half-baked submission row
        assert db_session.query(Submission).count() == 0
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.resolve_lockfile")
    def test_resolver_failure_emits_structured_rejection(
        self, mock_resolve, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """uv pip compile non-zero exit → 422 with lockfile_resolution_failed."""
        from app.services.lockfile_resolver import LockfileResolutionError
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [{"name": "nonexistent-pkg"}])
        mock_clone.return_value = repo
        mock_resolve.side_effect = LockfileResolutionError(
            "ERROR: No matching distribution found for nonexistent-pkg",
        )

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["result"] == "rejected"
        assert "lockfile_resolution_failed" in detail["reason_codes"]
        # Some message hint is surfaced for the author
        msg_blob = detail.get("message", "") + " " + " ".join(
            f.get("message", "") or "" for f in detail.get("findings", [])
        )
        assert "nonexistent-pkg" in msg_blob or "No matching" in msg_blob
        # No half-baked submission row
        assert db_session.query(Submission).count() == 0
        _teardown_quick_submit_auth()

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, return_value="testuser")
    @patch("app.api.submit.get_settings")
    @patch("app.api.submit.clone_repo")
    @patch("app.api.submit.validation_queue")
    @patch("app.api.submit.resolve_lockfile")
    def test_bundle_manifest_with_packages_resolves_once(
        self, mock_resolve, mock_queue, mock_clone, mock_settings, mock_verify,
        client, seed_data, db_session, tmp_path,
    ):
        """A bundle with components + top-level packages resolves a single lockfile."""
        _setup_quick_submit_auth(seed_data)
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5
        settings.rate_limit_disabled = False

        repo = _make_fake_repo_with_packages(tmp_path, [{"name": "requests"}])
        # Add a component declaration so it looks like a bundle
        manifest_path = repo / "jarvis_command.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest["components"] = [
            {"name": "test_quick", "type": "command", "path": "command.py"},
        ]
        manifest_path.write_text(yaml.dump(manifest))
        mock_clone.return_value = repo
        mock_queue.enqueue = AsyncMock()
        mock_resolve.return_value = _HAPPY_LOCKFILE

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "confirm": True,
        })
        assert resp.status_code == 200
        assert mock_resolve.call_count == 1
        args, kwargs = mock_resolve.call_args
        called_with = args[0] if args else kwargs.get("packages")
        assert list(called_with) == ["requests"]
        _teardown_quick_submit_auth()


class TestLockfileResolverHelper:
    """Direct unit tests on app.services.lockfile_resolver.resolve_lockfile()."""

    def test_returns_lockfile_string_on_success(self):
        from app.services import lockfile_resolver
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["uv", "pip", "compile"], returncode=0,
                stdout=_HAPPY_LOCKFILE, stderr="",
            )
            result = lockfile_resolver.resolve_lockfile(["requests", "pyyaml"])
        assert result == _HAPPY_LOCKFILE
        # Subprocess was called with uv + the package names piped via stdin
        args, kwargs = mock_run.call_args
        cmd = args[0] if args else kwargs.get("args")
        assert "uv" in cmd[0] or cmd[0] == "uv"
        assert "pip" in cmd
        assert "compile" in cmd
        # Packages arrive via stdin (the trailing "-" arg tells uv to read requirements from stdin)
        assert "-" in cmd
        stdin_input = kwargs.get("input", "")
        assert "requests" in stdin_input
        assert "pyyaml" in stdin_input

    def test_empty_packages_returns_empty_string_without_subprocess(self):
        from app.services import lockfile_resolver
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            result = lockfile_resolver.resolve_lockfile([])
        assert result == ""
        assert mock_run.call_count == 0

    def test_raises_resolution_error_on_non_zero_exit(self):
        from app.services import lockfile_resolver
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["uv"], returncode=1, stdout="",
                stderr="ERROR: No matching distribution found for nonexistent-pkg",
            )
            try:
                lockfile_resolver.resolve_lockfile(["nonexistent-pkg"])
            except lockfile_resolver.LockfileResolutionError as e:
                assert "nonexistent-pkg" in str(e) or "No matching" in str(e)
            else:
                raise AssertionError("expected LockfileResolutionError")

    def test_raises_too_large_on_oversize_output(self):
        from app.services import lockfile_resolver
        oversize = "a" * (lockfile_resolver.LOCKFILE_SIZE_CAP_BYTES + 1)
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["uv"], returncode=0, stdout=oversize, stderr="",
            )
            try:
                lockfile_resolver.resolve_lockfile(["requests"])
            except lockfile_resolver.LockfileTooLargeError:
                pass
            else:
                raise AssertionError("expected LockfileTooLargeError")

    def test_raises_resolution_error_on_subprocess_timeout(self):
        from app.services import lockfile_resolver
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="uv", timeout=30)
            try:
                lockfile_resolver.resolve_lockfile(["requests"])
            except lockfile_resolver.LockfileResolutionError:
                pass
            else:
                raise AssertionError("expected LockfileResolutionError on timeout")

    def test_propagates_filenotfound_when_uv_missing(self):
        from app.services import lockfile_resolver
        with patch("app.services.lockfile_resolver.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("uv")
            try:
                lockfile_resolver.resolve_lockfile(["requests"])
            except FileNotFoundError:
                pass
            except lockfile_resolver.LockfileResolutionError:
                pass
            else:
                raise AssertionError(
                    "expected FileNotFoundError or LockfileResolutionError",
                )

    def test_cap_constant_is_50kb(self):
        from app.services import lockfile_resolver
        assert lockfile_resolver.LOCKFILE_SIZE_CAP_BYTES == 50 * 1024
