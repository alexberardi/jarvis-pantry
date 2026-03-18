"""Tests for the submission endpoint."""

import json
from unittest.mock import patch, MagicMock, AsyncMock

from app.models import Author, Submission
from app.services.security_review import SecurityReviewResult


class TestSubmitCommand:
    def test_invalid_provider(self, client, seed_data):
        """Reject non-claude/openai providers."""
        with patch("app.api.submit.validate_github_token", return_value=seed_data["author"]):
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

        # Create a submission
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
