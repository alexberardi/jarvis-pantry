"""Tests for finalize_submission row fields + SDK-version auto-set.

Resilient-package-installs (Pantry side): every published CommandVersion
must pin what was validated — `git_tag` is never null for new rows,
`git_commit_sha` carries the validated HEAD, and `manifest_json` carries a
`min_sdk_version` floor (author-declared or auto-set from the validation
SDK).
"""

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models import Author, Command, CommandVersion, Submission
from app.services import finalize as finalize_mod
from app.services.container_test import ContainerTestResult
from app.services.finalize import (
    build_dispatch_context,
    ensure_min_sdk_version,
    finalize_submission,
    resolve_sdk_version,
)

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _passing_result() -> ContainerTestResult:
    return ContainerTestResult(
        passed=True, summary="2/2 passed", test_count=2, pass_count=2, fail_count=0,
    )


def _manifest(**overrides) -> dict:
    manifest = {
        "name": "widget",
        "display_name": "Widget",
        "description": "Does widget things",
        "version": "1.0.0",
        "categories": ["utility"],
        "platforms": ["linux"],
        "license": "MIT",
        "components": [{"name": "widget", "type": "command", "path": "command.py"}],
    }
    manifest.update(overrides)
    return manifest


def _seed_submission(db_session) -> Submission:
    author = Author(
        github_id=777, github_username="finalizer", display_name="Finalizer",
    )
    db_session.add(author)
    db_session.flush()
    sub = Submission(
        github_repo_url="https://github.com/test/jarvis-command-widget",
        author_id=author.id,
        status="container_test",
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    return sub


class TestFinalizeRowFields:
    def test_git_tag_and_sha_stored(self, db_session):
        sub = _seed_submission(db_session)
        with patch.object(finalize_mod, "resolve_sdk_version", return_value=None):
            finalize_submission(
                db=db_session,
                submission=sub,
                manifest=_manifest(),
                review=None,
                author_github="finalizer",
                repo_url=sub.github_repo_url,
                container_result=_passing_result(),
                git_commit_sha=FAKE_SHA,
            )
        ver = db_session.query(CommandVersion).filter(
            CommandVersion.version == "1.0.0",
        ).first()
        assert ver is not None
        assert ver.git_tag == "v1.0.0"  # never null for new rows
        assert ver.git_commit_sha == FAKE_SHA

    def test_missing_sha_still_writes_version_tag(self, db_session):
        """Archive-path clones have no SHA — git_tag still pins the version."""
        sub = _seed_submission(db_session)
        with patch.object(finalize_mod, "resolve_sdk_version", return_value=None):
            finalize_submission(
                db=db_session,
                submission=sub,
                manifest=_manifest(),
                review=None,
                author_github="finalizer",
                repo_url=sub.github_repo_url,
                container_result=_passing_result(),
                git_commit_sha=None,
            )
        ver = db_session.query(CommandVersion).filter(
            CommandVersion.version == "1.0.0",
        ).first()
        assert ver.git_tag == "v1.0.0"
        assert ver.git_commit_sha is None

    def test_min_sdk_version_auto_set_in_manifest_json(self, db_session):
        sub = _seed_submission(db_session)
        with patch.object(finalize_mod, "resolve_sdk_version", return_value="0.3.3"):
            finalize_submission(
                db=db_session,
                submission=sub,
                manifest=_manifest(),
                review=None,
                author_github="finalizer",
                repo_url=sub.github_repo_url,
                container_result=_passing_result(),
                git_commit_sha=FAKE_SHA,
            )
        ver = db_session.query(CommandVersion).filter(
            CommandVersion.version == "1.0.0",
        ).first()
        assert ver.manifest_json["min_sdk_version"] == "0.3.3"

    def test_reject_path_skips_min_sdk_auto_set(self, db_session):
        """No rows are written on rejection — the manifest stays untouched."""
        sub = _seed_submission(db_session)
        manifest = _manifest()
        failing = ContainerTestResult(
            passed=False, summary="1/2 failed", test_count=2, pass_count=1, fail_count=1,
        )
        with patch.object(
            finalize_mod, "resolve_sdk_version", return_value="0.3.3",
        ) as mock_resolve:
            finalize_submission(
                db=db_session,
                submission=sub,
                manifest=manifest,
                review=None,
                author_github="finalizer",
                repo_url=sub.github_repo_url,
                container_result=failing,
                git_commit_sha=FAKE_SHA,
            )
        assert sub.status == "rejected"
        assert mock_resolve.call_count == 0
        assert "min_sdk_version" not in manifest
        assert db_session.query(CommandVersion).count() == 0


class TestEnsureMinSdkVersion:
    def test_absent_sets_resolved_version(self):
        manifest = _manifest()
        with patch.object(finalize_mod, "resolve_sdk_version", return_value="0.3.3"):
            ensure_min_sdk_version(manifest)
        assert manifest["min_sdk_version"] == "0.3.3"

    def test_author_declared_floor_untouched(self):
        manifest = _manifest(min_sdk_version="0.2.0")
        with patch.object(finalize_mod, "resolve_sdk_version", return_value="0.3.3"):
            ensure_min_sdk_version(manifest)
        assert manifest["min_sdk_version"] == "0.2.0"

    def test_sdk_import_failure_leaves_field_absent(self):
        manifest = _manifest()
        with patch.object(finalize_mod, "resolve_sdk_version", return_value=None):
            ensure_min_sdk_version(manifest)
        assert "min_sdk_version" not in manifest


class TestResolveSdkVersion:
    def _make_fake_sdk(self, tmp_path, version="9.9.9", with_relative_import=False):
        pkg = tmp_path / "sdk" / "jarvis_command_sdk"
        pkg.mkdir(parents=True)
        lines = []
        if with_relative_import:
            (pkg / "helper.py").write_text('VALUE = "x"\n')
            lines.append("from .helper import VALUE")
        lines.append(f'__version__ = "{version}"')
        (pkg / "__init__.py").write_text("\n".join(lines) + "\n")
        return tmp_path / "sdk"

    def test_reads_version_from_sdk_path(self, monkeypatch, tmp_path):
        sdk_root = self._make_fake_sdk(tmp_path, version="9.9.9")
        monkeypatch.setattr(
            finalize_mod, "get_settings",
            lambda: SimpleNamespace(sdk_path=str(sdk_root)),
        )
        assert resolve_sdk_version() == "9.9.9"
        # The probe must not leave the fake SDK shadowing real imports.
        probe = sys.modules.get("jarvis_command_sdk")
        assert probe is None or "9.9.9" != getattr(probe, "__version__", None)

    def test_resolves_sdk_with_relative_imports(self, monkeypatch, tmp_path):
        """The real SDK __init__ uses relative imports — registration during
        exec must make them resolve."""
        sdk_root = self._make_fake_sdk(tmp_path, version="1.2.3", with_relative_import=True)
        monkeypatch.setattr(
            finalize_mod, "get_settings",
            lambda: SimpleNamespace(sdk_path=str(sdk_root)),
        )
        assert resolve_sdk_version() == "1.2.3"

    def test_returns_none_when_sdk_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            finalize_mod, "get_settings",
            lambda: SimpleNamespace(sdk_path=str(tmp_path / "missing")),
        )
        real_import_module = importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name == "jarvis_command_sdk":
                raise ImportError("not installed")
            return real_import_module(name, *args, **kwargs)

        monkeypatch.setattr(finalize_mod.importlib, "import_module", fake_import)
        assert resolve_sdk_version() is None


class TestBuildDispatchContext:
    def test_carries_git_commit_sha(self):
        ctx = build_dispatch_context(
            manifest=_manifest(),
            review=None,
            author_github="finalizer",
            repo_url="https://github.com/test/jarvis-command-widget",
            git_commit_sha=FAKE_SHA,
        )
        assert ctx["git_commit_sha"] == FAKE_SHA

    def test_sha_defaults_to_none(self):
        ctx = build_dispatch_context(
            manifest=_manifest(),
            review=None,
            author_github="finalizer",
            repo_url="https://github.com/test/jarvis-command-widget",
        )
        assert ctx["git_commit_sha"] is None


class TestFinalizeNameOwnership:
    """finalize re-checks ownership: another author may have taken the name
    while the submission waited in the queue."""

    def _existing(self, db_session, author_id: int) -> Command:
        cmd = Command(
            command_name="widget", display_name="Widget", description="d",
            github_repo_url="https://github.com/original/jarvis-command-widget",
            author_id=author_id, latest_version="0.9.0",
        )
        db_session.add(cmd)
        db_session.commit()
        return cmd

    def _finalize(self, db_session, sub):
        with patch.object(finalize_mod, "resolve_sdk_version", return_value=None):
            finalize_submission(
                db=db_session, submission=sub, manifest=_manifest(), review=None,
                author_github="finalizer", repo_url=sub.github_repo_url,
                container_result=_passing_result(), git_commit_sha=FAKE_SHA,
            )

    def test_other_authors_package_is_not_repointed(self, db_session):
        owner = Author(github_id=1001, github_username="original", display_name="Original")
        db_session.add(owner)
        db_session.flush()
        cmd = self._existing(db_session, owner.id)
        sub = _seed_submission(db_session)
        self._finalize(db_session, sub)
        db_session.refresh(cmd)
        assert sub.status == "rejected"
        assert "belongs to another author" in sub.error_message
        assert cmd.github_repo_url == "https://github.com/original/jarvis-command-widget"
        assert cmd.latest_version == "0.9.0"
        assert db_session.query(CommandVersion).filter(CommandVersion.command_id == cmd.id).count() == 0

    def test_owner_publishes_a_new_version(self, db_session):
        sub = _seed_submission(db_session)
        cmd = self._existing(db_session, sub.author_id)
        self._finalize(db_session, sub)
        db_session.refresh(cmd)
        assert sub.status == "published"
        assert cmd.latest_version == "1.0.0"
        assert cmd.github_repo_url == sub.github_repo_url

    def test_operator_may_publish_any_package(self, db_session):
        owner = Author(github_id=1001, github_username="original", display_name="Original")
        operator = Author(github_id=0, github_username=finalize_mod.OPERATOR_GITHUB_USERNAME,
                          display_name="Pantry Operator")
        db_session.add_all([owner, operator])
        db_session.flush()
        cmd = self._existing(db_session, owner.id)
        sub = Submission(github_repo_url="https://github.com/original/jarvis-command-widget",
                         author_id=operator.id, status="container_test")
        db_session.add(sub)
        db_session.commit()
        self._finalize(db_session, sub)
        assert sub.status == "published"
        assert cmd.author_id == owner.id  # authorship unchanged

    def test_a_github_user_named_like_the_operator_is_not_the_operator(self, db_session):
        owner = Author(github_id=1001, github_username="original", display_name="Original")
        impostor = Author(github_id=5555, github_username=finalize_mod.OPERATOR_GITHUB_USERNAME,
                          display_name="x")
        db_session.add_all([owner, impostor])
        db_session.flush()
        self._existing(db_session, owner.id)
        assert not finalize_mod.may_publish_to(
            db_session, db_session.query(Command).filter(Command.command_name == "widget").one(), impostor.id)
