"""Tests for the in-process submission pipeline's CommandVersion row fields.

The legacy POST /v1/commands path publishes synchronously; its
CommandVersion rows must pin the validated commit SHA and carry the
min_sdk_version floor exactly like the async finalize path.
"""

from unittest.mock import AsyncMock, patch

import yaml

from app.models import Author, CommandVersion
from app.services.security_review import SecurityReviewResult
from app.services.submission_pipeline import process_submission

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"

COMMAND_PY = """\
class WidgetCommand:
    def run(self, request_info, **kwargs):
        return {"status": "ok"}
"""


def _make_repo(tmp_path, manifest_overrides=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = {"name": "widget", "description": "Does widget things", "version": "1.0.0"}
    manifest.update(manifest_overrides or {})
    (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
    (repo / "command.py").write_text(COMMAND_PY)
    (repo / "README.md").write_text("# Widget")
    (repo / "LICENSE").write_text("MIT")
    return repo


def _seed_author(db_session) -> Author:
    author = Author(
        github_id=888, github_username="pipeliner", display_name="Pipeliner",
    )
    db_session.add(author)
    db_session.commit()
    db_session.refresh(author)
    return author


def _approving_review() -> SecurityReviewResult:
    return SecurityReviewResult(
        danger_score=1,
        summary="Safe",
        concerns=[],
        recommendation="approve",
        raw_response={},
    )


class TestProcessSubmissionRowFields:
    async def test_stores_sha_and_version_tag(self, db_session, tmp_path):
        author = _seed_author(db_session)
        repo = _make_repo(tmp_path)

        with patch(
            "app.services.submission_pipeline.clone_repo",
            return_value=(repo, FAKE_SHA),
        ), patch(
            "app.services.submission_pipeline.run_security_review",
            new=AsyncMock(return_value=_approving_review()),
        ), patch(
            "app.services.finalize.resolve_sdk_version", return_value=None,
        ):
            result = await process_submission(
                repo_url="https://github.com/test/jarvis-command-widget",
                llm_provider="claude",
                llm_api_key="sk-test",
                author=author,
                db=db_session,
            )

        assert result["status"] == "published"
        ver = db_session.query(CommandVersion).filter(
            CommandVersion.version == "1.0.0",
        ).first()
        assert ver is not None
        assert ver.git_tag == "v1.0.0"
        assert ver.git_commit_sha == FAKE_SHA

    async def test_min_sdk_version_auto_set(self, db_session, tmp_path):
        author = _seed_author(db_session)
        repo = _make_repo(tmp_path)

        with patch(
            "app.services.submission_pipeline.clone_repo",
            return_value=(repo, FAKE_SHA),
        ), patch(
            "app.services.submission_pipeline.run_security_review",
            new=AsyncMock(return_value=_approving_review()),
        ), patch(
            "app.services.finalize.resolve_sdk_version", return_value="0.3.3",
        ):
            await process_submission(
                repo_url="https://github.com/test/jarvis-command-widget",
                llm_provider="claude",
                llm_api_key="sk-test",
                author=author,
                db=db_session,
            )

        ver = db_session.query(CommandVersion).first()
        assert ver.manifest_json["min_sdk_version"] == "0.3.3"

    async def test_author_declared_min_sdk_version_kept(self, db_session, tmp_path):
        author = _seed_author(db_session)
        repo = _make_repo(tmp_path, {"min_sdk_version": "0.2.0"})

        with patch(
            "app.services.submission_pipeline.clone_repo",
            return_value=(repo, FAKE_SHA),
        ), patch(
            "app.services.submission_pipeline.run_security_review",
            new=AsyncMock(return_value=_approving_review()),
        ), patch(
            "app.services.finalize.resolve_sdk_version", return_value="0.3.3",
        ):
            await process_submission(
                repo_url="https://github.com/test/jarvis-command-widget",
                llm_provider="claude",
                llm_api_key="sk-test",
                author=author,
                db=db_session,
            )

        ver = db_session.query(CommandVersion).first()
        assert ver.manifest_json["min_sdk_version"] == "0.2.0"

    async def test_missing_sha_stores_null_sha(self, db_session, tmp_path):
        author = _seed_author(db_session)
        repo = _make_repo(tmp_path)

        with patch(
            "app.services.submission_pipeline.clone_repo",
            return_value=(repo, None),
        ), patch(
            "app.services.submission_pipeline.run_security_review",
            new=AsyncMock(return_value=_approving_review()),
        ), patch(
            "app.services.finalize.resolve_sdk_version", return_value=None,
        ):
            await process_submission(
                repo_url="https://github.com/test/jarvis-command-widget",
                llm_provider="claude",
                llm_api_key="sk-test",
                author=author,
                db=db_session,
            )

        ver = db_session.query(CommandVersion).first()
        assert ver.git_tag == "v1.0.0"
        assert ver.git_commit_sha is None
