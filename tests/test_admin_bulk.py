"""Tests for the operator bulk-submission endpoints (X-Admin-Key gated)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)


ADMIN_KEY = "test-admin-key-123"


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def admin_settings(monkeypatch):
    """Set ADMIN_API_KEY in env, clear the settings cache so the endpoint sees it."""
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_pipeline(monkeypatch, tmp_path, db_session):
    """Stub out clone/validate/static_analysis/lockfile/queue so the bulk
    endpoint's per-repo orchestration runs without touching git or the LLM.

    Also redirects `SessionLocal()` calls inside `_process_one` to the test
    db_session — without this, parallel tasks would create real sessions
    against the production engine and try to hit the configured DATABASE_URL.
    """
    enqueued: list = []

    class _SessionWrapper:
        """Wraps the test session so .close() is a no-op (the fixture owns it)."""
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            return getattr(self._inner, name)
        def close(self):
            pass

    monkeypatch.setattr(
        "app.api.admin_bulk.SessionLocal",
        lambda: _SessionWrapper(db_session),
    )

    def fake_clone(repo_url, tag=None):
        d = tmp_path / "clones" / repo_url.replace("/", "_").replace(":", "_")
        d.mkdir(parents=True, exist_ok=True)
        return d, "a" * 40

    def fake_validate(repo_dir):
        # Mimic the manifest shape consumers downstream rely on.
        return {
            "name": f"cmd_{repo_dir.name[-8:]}",
            "version": "0.1.0",
            "description": "fake",
            "components": [{"name": "cmd", "type": "command", "path": "command.py"}],
        }

    def fake_static_analysis(repo_dir):
        # Returns an object with .passed, .reason_codes, .to_dict() per StaticAnalysisResult.
        class Result:
            passed = True
            reason_codes: list[str] = []
            def to_dict(self):
                return {"passed": True, "checks_passed": 1, "findings": [], "warnings": [], "reason_codes": []}
        return Result()

    def fake_resolve(specs):
        return ""  # empty lockfile

    def fake_cleanup(repo_dir):
        pass

    async def fake_enqueue(job):
        enqueued.append(job)

    monkeypatch.setattr("app.api.admin_bulk.clone_repo", fake_clone)
    monkeypatch.setattr("app.api.admin_bulk.validate_structure", fake_validate)
    monkeypatch.setattr("app.api.admin_bulk.run_static_analysis", fake_static_analysis)
    monkeypatch.setattr("app.api.admin_bulk.resolve_lockfile", fake_resolve)
    monkeypatch.setattr("app.api.admin_bulk.cleanup_repo", fake_cleanup)
    monkeypatch.setattr("app.api.admin_bulk.validation_queue.enqueue", fake_enqueue)
    yield enqueued


# ── Admin-key gating ────────────────────────────────────────────────────


class TestAdminKeyGating:
    def test_post_rejects_missing_header(self, client, admin_settings):
        resp = client.post("/v1/admin/bulk-submissions", json={
            "repos": [{"owner_repo": "foo/bar"}],
            "llm_provider": "claude",
            "llm_api_key": "test",
        })
        # FastAPI returns 422 when a required header is missing.
        assert resp.status_code in (422, 401, 403)

    def test_post_rejects_wrong_header(self, client, admin_settings):
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [{"owner_repo": "foo/bar"}],
                "llm_provider": "claude",
                "llm_api_key": "test",
            },
            headers={"X-Admin-Key": "wrong-key"},
        )
        assert resp.status_code == 403

    def test_get_rejects_wrong_header(self, client, admin_settings):
        resp = client.get(
            "/v1/admin/bulk-submissions/nope",
            headers={"X-Admin-Key": "wrong-key"},
        )
        assert resp.status_code == 403

    def test_post_rejects_when_server_key_unset(self, client, monkeypatch):
        """Missing ADMIN_API_KEY on the server must NOT mean public access."""
        monkeypatch.setenv("ADMIN_API_KEY", "")
        from app.config import get_settings
        get_settings.cache_clear()
        try:
            resp = client.post(
                "/v1/admin/bulk-submissions",
                json={
                    "repos": [{"owner_repo": "foo/bar"}],
                    "llm_provider": "claude",
                    "llm_api_key": "test",
                },
                headers={"X-Admin-Key": "anything"},
            )
            assert resp.status_code == 403
        finally:
            get_settings.cache_clear()


# ── Submit ──────────────────────────────────────────────────────────────


class TestBulkSubmit:
    def test_accepts_valid_repos(self, client, admin_settings, mock_pipeline, db_session):
        from app.models import Submission

        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [
                    {"owner_repo": "alice/cmd-one"},
                    {"owner_repo": "bob/cmd-two"},
                ],
                "llm_provider": "claude",
                "llm_api_key": "byok-test",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["accepted_count"] == 2
        assert data["rejected_count"] == 0
        assert len(data["outcomes"]) == 2
        batch_id = data["batch_id"]
        assert batch_id and len(batch_id) >= 16

        # All rows must be tagged with the shared batch_id.
        rows = db_session.query(Submission).filter(Submission.batch_id == batch_id).all()
        assert len(rows) == 2
        assert {r.github_repo_url for r in rows} == {
            "https://github.com/alice/cmd-one",
            "https://github.com/bob/cmd-two",
        }

        # Each row must have been enqueued for async processing.
        assert len(mock_pipeline) == 2

    def test_normalizes_full_https_url(self, client, admin_settings, mock_pipeline):
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [{"owner_repo": "https://github.com/alice/cmd-one/"}],
                "llm_provider": "claude",
                "llm_api_key": "byok-test",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        outcome = resp.json()["outcomes"][0]
        assert outcome["owner_repo"] == "alice/cmd-one"

    def test_static_analysis_rejection_still_creates_row(
        self, client, admin_settings, monkeypatch, mock_pipeline, db_session,
    ):
        """A repo that fails static analysis should still appear in the batch
        status — the operator polls one endpoint for the whole picture."""
        class FailingResult:
            passed = False
            reason_codes = ["bad_import"]
            def to_dict(self):
                return {"passed": False, "reason_codes": ["bad_import"], "findings": [], "warnings": []}
        monkeypatch.setattr(
            "app.api.admin_bulk.run_static_analysis",
            lambda repo_dir: FailingResult(),
        )

        from app.models import Submission
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [{"owner_repo": "bad/repo"}],
                "llm_provider": "claude",
                "llm_api_key": "byok-test",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted_count"] == 0
        assert data["rejected_count"] == 1
        assert "Static analysis failed" in data["outcomes"][0]["reason"]

        # Rejected entries still get a Submission row tagged with batch_id.
        rows = db_session.query(Submission).filter(Submission.batch_id == data["batch_id"]).all()
        assert len(rows) == 1
        assert rows[0].status == "rejected"
        assert rows[0].error_message and "Static analysis failed" in rows[0].error_message

    def test_clone_failure_still_creates_row(
        self, client, admin_settings, monkeypatch, mock_pipeline, db_session,
    ):
        from app.services.github_service import RepoValidationError
        def boom(repo_url, tag=None):
            raise RepoValidationError("git clone failed: no such repo")
        monkeypatch.setattr("app.api.admin_bulk.clone_repo", boom)

        from app.models import Submission
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [{"owner_repo": "ghost/repo"}],
                "llm_provider": "claude",
                "llm_api_key": "byok-test",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected_count"] == 1
        rows = db_session.query(Submission).filter(Submission.batch_id == data["batch_id"]).all()
        assert len(rows) == 1
        assert rows[0].status == "rejected"

    def test_bad_provider_rejected_before_processing(self, client, admin_settings, mock_pipeline):
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [{"owner_repo": "foo/bar"}],
                "llm_provider": "garbage",
                "llm_api_key": "x",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 400

    def test_empty_repos_rejected(self, client, admin_settings, mock_pipeline):
        resp = client.post(
            "/v1/admin/bulk-submissions",
            json={"repos": [], "llm_provider": "claude", "llm_api_key": "x"},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 422  # pydantic min_length=1


# ── Status ──────────────────────────────────────────────────────────────


class TestBulkStatus:
    def test_404_for_unknown_batch(self, client, admin_settings):
        resp = client.get(
            "/v1/admin/bulk-submissions/does-not-exist",
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 404

    def test_returns_rows_for_known_batch(
        self, client, admin_settings, mock_pipeline, db_session,
    ):
        post = client.post(
            "/v1/admin/bulk-submissions",
            json={
                "repos": [
                    {"owner_repo": "alice/one"},
                    {"owner_repo": "bob/two"},
                ],
                "llm_provider": "claude",
                "llm_api_key": "byok-test",
            },
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert post.status_code == 200, post.text
        batch_id = post.json()["batch_id"]

        resp = client.get(
            f"/v1/admin/bulk-submissions/{batch_id}",
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["batch_id"] == batch_id
        assert data["total"] == 2
        assert len(data["submissions"]) == 2
        assert "by_status" in data


# ── Migration ───────────────────────────────────────────────────────────


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "h5d9e1f3a6b7_add_batch_id_to_submissions.py"
)


def _load_batch_id_migration():
    spec = importlib.util.spec_from_file_location("batch_id_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBatchIdMigration:
    @pytest.fixture
    def engine(self, tmp_path):
        eng = create_engine(f"sqlite:///{tmp_path / 'migrate.db'}")
        md = MetaData()
        Table(
            "submissions",
            md,
            Column("id", Integer, primary_key=True),
            Column("github_repo_url", String(512), nullable=False),
        )
        md.create_all(eng)
        try:
            yield eng
        finally:
            eng.dispose()

    def test_upgrade_adds_batch_id_column(self, engine):
        migration = _load_batch_id_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

        cols = {c["name"] for c in inspect(engine).get_columns("submissions")}
        assert "batch_id" in cols

    def test_downgrade_drops_batch_id_column(self, engine):
        migration = _load_batch_id_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.downgrade()

        cols = {c["name"] for c in inspect(engine).get_columns("submissions")}
        assert "batch_id" not in cols

    def test_existing_rows_preserved_through_upgrade(self, engine):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO submissions (id, github_repo_url) "
                "VALUES (1, 'https://github.com/foo/bar')",
            ))
        migration = _load_batch_id_migration()
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT id, github_repo_url, batch_id FROM submissions WHERE id = 1",
            )).first()
        assert row is not None
        assert row[1] == "https://github.com/foo/bar"
        assert row[2] is None  # nullable; pre-existing rows have no batch
