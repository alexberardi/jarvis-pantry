"""Tests for the download endpoint's install pinning.

`git_tag` in the response must pin the node to what Pantry validated:
the stored tag first, the validated commit SHA as fallback. Old rows with
both null are grandfathered (floating main) until resubmitted. The
response also surfaces the manifest's `min_sdk_version` floor.
"""

from app.models import CommandVersion

FAKE_SHA = "0123456789abcdef0123456789abcdef01234567"


class TestDownloadPinning:
    def test_git_tag_returned_when_set(self, client, seed_data):
        resp = client.get("/v1/commands/get_stock_price/download")
        assert resp.status_code == 200
        assert resp.json()["git_tag"] == "v1.0.0"

    def test_null_tag_falls_back_to_commit_sha(self, client, seed_data, db_session):
        ver = CommandVersion(
            command_id=1,
            version="1.1.0",
            git_tag=None,
            git_commit_sha=FAKE_SHA,
            manifest_json={"name": "get_stock_price", "version": "1.1.0"},
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(ver)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price/download?version=1.1.0")
        assert resp.status_code == 200
        assert resp.json()["git_tag"] == FAKE_SHA

    def test_grandfathered_rows_stay_null(self, client, seed_data, db_session):
        """Pre-pinning rows (tag AND sha null) keep floating until resubmitted."""
        ver = CommandVersion(
            command_id=1,
            version="0.9.0",
            git_tag=None,
            git_commit_sha=None,
            manifest_json={"name": "get_stock_price", "version": "0.9.0"},
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(ver)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price/download?version=0.9.0")
        assert resp.status_code == 200
        assert resp.json()["git_tag"] is None

    def test_tag_wins_over_sha(self, client, seed_data, db_session):
        ver = CommandVersion(
            command_id=1,
            version="1.2.0",
            git_tag="v1.2.0",
            git_commit_sha=FAKE_SHA,
            manifest_json={"name": "get_stock_price", "version": "1.2.0"},
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(ver)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price/download?version=1.2.0")
        assert resp.status_code == 200
        assert resp.json()["git_tag"] == "v1.2.0"


class TestDownloadMinSdkVersion:
    def test_min_sdk_version_surfaced_from_manifest(self, client, seed_data, db_session):
        ver = CommandVersion(
            command_id=1,
            version="1.3.0",
            git_tag="v1.3.0",
            manifest_json={
                "name": "get_stock_price",
                "version": "1.3.0",
                "min_sdk_version": "0.3.3",
            },
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(ver)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price/download?version=1.3.0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["min_sdk_version"] == "0.3.3"
        assert body["manifest"]["min_sdk_version"] == "0.3.3"

    def test_min_sdk_version_null_when_absent(self, client, seed_data):
        resp = client.get("/v1/commands/get_stock_price/download")
        assert resp.status_code == 200
        assert resp.json()["min_sdk_version"] is None

    def test_min_sdk_version_null_when_manifest_null(self, client, seed_data, db_session):
        ver = CommandVersion(
            command_id=1,
            version="1.4.0",
            git_tag="v1.4.0",
            manifest_json=None,
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(ver)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price/download?version=1.4.0")
        assert resp.status_code == 200
        assert resp.json()["min_sdk_version"] is None
