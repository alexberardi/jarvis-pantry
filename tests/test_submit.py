"""Tests for the submission endpoints."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import yaml

from app.models import Author, Command, Submission
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
    (repo / "command.py").write_text("# test command")
    (repo / "README.md").write_text("# Test")
    (repo / "LICENSE").write_text("MIT")
    return repo


class TestQuickSubmit:
    def test_invalid_url(self, client):
        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://gitlab.com/test/repo",
        })
        assert resp.status_code == 400

    @patch("app.api.submit.clone_repo")
    def test_success(self, mock_clone, client, db_session, tmp_path):
        repo = _make_fake_repo(tmp_path)
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/jarvis-command-test",
            "author_github": "testuser",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["command_name"] == "test_quick"
        assert data["version"] == "1.0.0"

        # Verify it's in the database
        cmd = db_session.query(Command).filter(
            Command.command_name == "test_quick"
        ).first()
        assert cmd is not None
        assert cmd.published is True

    @patch("app.api.submit.clone_repo")
    def test_missing_files(self, mock_clone, client, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        (repo / "command.py").write_text("# no manifest")
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/repo",
        })
        assert resp.status_code == 422
        assert "Missing" in resp.json()["detail"]

    @patch("app.api.submit.clone_repo")
    def test_upsert_existing(self, mock_clone, client, seed_data, db_session, tmp_path):
        """Re-submitting an existing command updates it."""
        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        manifest = {
            "name": "get_stock_price",
            "description": "Updated description",
            "version": "2.0.0",
            "display_name": "Stock Price v2",
        }
        (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
        (repo / "command.py").write_text("# updated")
        (repo / "README.md").write_text("# Updated")
        (repo / "LICENSE").write_text("MIT")
        mock_clone.return_value = repo

        resp = client.post("/v1/commands/quick-submit", json={
            "repo_url": "https://github.com/test/repo",
        })
        assert resp.status_code == 200
        assert resp.json()["version"] == "2.0.0"

        cmd = db_session.query(Command).filter(
            Command.command_name == "get_stock_price"
        ).first()
        assert cmd.latest_version == "2.0.0"
        assert cmd.description == "Updated description"
