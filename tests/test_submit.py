"""Tests for the submission endpoints."""

from pathlib import Path
from unittest.mock import patch, AsyncMock

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
        assert any("SyntaxError" in e for e in detail["errors"])
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

    @patch("app.api.submit.verify_repo_access", new_callable=AsyncMock, side_effect=RepoValidationError("You don't have push access"))
    @patch("app.api.submit.get_settings")
    def test_repo_access_denied(self, mock_settings, mock_verify, client, seed_data):
        """Submitting a repo you don't own returns 403."""
        settings = mock_settings.return_value
        settings.bypass_llm_key = True
        settings.submission_rate_limit_per_hour = 100
        settings.submission_rate_limit_per_user_per_hour = 100
        settings.max_concurrent_clones = 5

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
