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
