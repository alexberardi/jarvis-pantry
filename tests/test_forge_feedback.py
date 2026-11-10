"""Send to Forge: a node's test-install report attached to a draft, read back by the Forge session."""

from datetime import datetime, timedelta, timezone

from app.api import forge_drafts
from app.models import ForgeDraft


def _draft(client, session_id="sess-1"):
    resp = client.post("/v1/forge/drafts", json={
        "session_id": session_id,
        "package_name": "countdown",
        "files": [{"filename": "command.py", "content": "x", "language": "python"}],
    })
    assert resp.status_code == 200
    return resp.json()["share_code"]


REPORT = {
    "install": {"status": "completed", "error": None, "details": {"commands": ["days_until_date"]}},
    "events": [{"seq": 1, "kind": "log", "level": "WARNING", "message": "date missing"}],
    "runs": [{"command": "days_until_date", "arguments": '{"date": "2026-12-25"}',
              "output": {"success": False, "error": "KeyError: 'date'"}, "at": "2026-11-09T10:00:00Z"}],
    "note": "it crashed on Christmas",
}


class TestFeedback:
    def test_report_reaches_the_session(self, client):
        code = _draft(client)
        assert client.post(f"/v1/forge/drafts/{code.lower()}/feedback", json=REPORT).status_code == 201

        reports = client.get("/v1/forge/sessions/sess-1/feedback").json()["reports"]
        assert len(reports) == 1
        assert reports[0]["runs"][0]["output"]["error"] == "KeyError: 'date'"
        assert reports[0]["note"] == "it crashed on Christmas"
        assert reports[0]["events"][0]["message"] == "date missing"
        assert reports[0]["share_code"] == code
        assert reports[0]["received_at"]

    def test_other_sessions_see_nothing(self, client):
        code = _draft(client)
        client.post(f"/v1/forge/drafts/{code}/feedback", json=REPORT)
        assert client.get("/v1/forge/sessions/other/feedback").json()["reports"] == []

    def test_unknown_code_404(self, client):
        assert client.post("/v1/forge/drafts/ZZZZZZ/feedback", json=REPORT).status_code == 404

    def test_expired_draft_still_takes_a_report(self, client, db_session):
        # The node's 20-minute test window outlives the draft's 15-minute share code.
        code = _draft(client)
        d = db_session.query(ForgeDraft).filter_by(share_code=code).one()
        d.expires_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        db_session.commit()
        assert client.post(f"/v1/forge/drafts/{code}/feedback", json=REPORT).status_code == 201

    def test_keeps_only_the_newest_reports(self, client):
        code = _draft(client)
        for i in range(forge_drafts._MAX_REPORTS + 3):
            client.post(f"/v1/forge/drafts/{code}/feedback", json={**REPORT, "note": str(i)})
        reports = client.get("/v1/forge/sessions/sess-1/feedback").json()["reports"]
        assert len(reports) == forge_drafts._MAX_REPORTS
        assert reports[-1]["note"] == str(forge_drafts._MAX_REPORTS + 2)

    def test_oversized_report_rejected(self, client):
        code = _draft(client)
        big = {**REPORT, "runs": [{"command": "x", "output": "y" * (forge_drafts._MAX_REPORT_BYTES + 1)}]}
        assert client.post(f"/v1/forge/drafts/{code}/feedback", json=big).status_code == 413


class TestCleanup:
    def test_recently_expired_drafts_are_kept_for_reports(self, db_session):
        now = datetime.now(timezone.utc)
        for code, ago in (("AAAAAA", 10), ("BBBBBB", 120)):
            db_session.add(ForgeDraft(share_code=code, session_id=code, package_name="p", files_json=[],
                                      expires_at=now - timedelta(minutes=ago)))
        db_session.commit()
        assert forge_drafts.cleanup_expired_drafts(db_session) == 1
        assert db_session.query(ForgeDraft).one().share_code == "AAAAAA"


class TestDraftRateLimits:
    """Drafts need no login (the share code is the secret), so reads and writes are capped per IP."""

    def test_guessing_share_codes_is_capped(self, client, monkeypatch):
        monkeypatch.setattr(forge_drafts._draft_read_limiter, "requests_per_hour", 2)
        statuses = [client.get(f"/v1/forge/drafts/ZZZZZ{c}").status_code for c in "ABC"]
        assert statuses == [404, 404, 429]

    def test_saving_drafts_is_capped(self, client, monkeypatch):
        monkeypatch.setattr(forge_drafts._draft_write_limiter, "requests_per_hour", 1)
        _draft(client)
        resp = client.post("/v1/forge/drafts", json={
            "session_id": "sess-2", "package_name": "p",
            "files": [{"filename": "command.py", "content": "x", "language": "python"}],
        })
        assert resp.status_code == 429
