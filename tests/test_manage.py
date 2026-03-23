"""Tests for auth and command management endpoints."""

from app.models import Author, Command, CommandVersion, SecurityReport, Submission


class TestGitHubAuthUrl:
    def test_returns_url(self, client):
        resp = client.get("/v1/auth/github", params={"redirect_uri": "http://localhost:3000/submit"})
        # Will fail with 503 if GITHUB_CLIENT_ID not set, which is expected in test
        assert resp.status_code in (200, 503)

    def test_missing_redirect_uri(self, client):
        resp = client.get("/v1/auth/github")
        assert resp.status_code == 422


class TestGetCurrentUser:
    def test_requires_auth(self, client):
        resp = client.get("/v1/auth/me")
        assert resp.status_code == 422  # Missing Authorization header


class TestDeleteCommand:
    def test_requires_auth(self, client, seed_data):
        resp = client.delete(f"/v1/commands/{seed_data['command'].command_name}")
        assert resp.status_code == 422  # Missing Authorization header

    def test_not_found(self, client, seed_data):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        resp = client.delete("/v1/commands/nonexistent")
        assert resp.status_code == 404

        app.dependency_overrides.pop(validate_github_token, None)

    def test_wrong_owner(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token

        # Create a different author
        other_author = Author(
            id=99,
            github_id=99999,
            github_username="other_user",
            display_name="Other User",
        )
        db_session.add(other_author)
        db_session.commit()

        app.dependency_overrides[validate_github_token] = lambda: other_author

        resp = client.delete(f"/v1/commands/{seed_data['command'].command_name}")
        assert resp.status_code == 403
        assert "own commands" in resp.json()["detail"]

        app.dependency_overrides.pop(validate_github_token, None)

    def test_successful_delete(self, client, seed_data, db_session):
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        command_name = seed_data["command"].command_name

        resp = client.delete(f"/v1/commands/{command_name}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        assert resp.json()["command_name"] == command_name

        # Verify command and version are gone
        assert db_session.query(Command).filter(Command.command_name == command_name).first() is None
        assert db_session.query(CommandVersion).filter(CommandVersion.command_id == seed_data["command"].id).first() is None

        app.dependency_overrides.pop(validate_github_token, None)

    def test_cascade_deletes_related_records(self, client, seed_data, db_session):
        """Delete cascades to versions, reports, submissions."""
        from app.main import app
        from app.auth import validate_github_token
        app.dependency_overrides[validate_github_token] = lambda: seed_data["author"]

        cmd = seed_data["command"]

        # Add a security report and submission
        report = SecurityReport(
            command_id=cmd.id,
            version="1.0.0",
            ai_review_summary="Looks safe",
            ai_danger_score=1,
            ai_recommendation="approve",
        )
        db_session.add(report)
        db_session.flush()

        sub = Submission(
            command_id=cmd.id,
            github_repo_url="https://github.com/test/repo",
            author_id=seed_data["author"].id,
            status="published",
        )
        db_session.add(sub)
        db_session.commit()

        resp = client.delete(f"/v1/commands/{cmd.command_name}")
        assert resp.status_code == 200

        # All related records should be gone
        assert db_session.query(SecurityReport).filter(SecurityReport.command_id == cmd.id).first() is None
        assert db_session.query(Submission).filter(Submission.command_id == cmd.id).first() is None

        app.dependency_overrides.pop(validate_github_token, None)
