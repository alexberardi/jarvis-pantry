"""Tests for command detail endpoints."""

from app.models import CommandVersion


class TestGetCommand:
    def test_not_found(self, client):
        resp = client.get("/v1/commands/nonexistent")
        assert resp.status_code == 404

    def test_found(self, client, seed_data):
        resp = client.get("/v1/commands/get_stock_price")
        assert resp.status_code == 200
        data = resp.json()
        assert data["command_name"] == "get_stock_price"
        assert data["display_name"] == "Stock Price Checker"
        assert data["verified"] is True
        assert data["install_count"] == 42
        assert data["author"]["github"] == "testuser"
        assert "finance" in data["categories"]


class TestAptPackagesInDetailResponse:
    """The detail response exposes `apt_packages` from the latest version's
    manifest so the mobile consent UI (#24) can read it. Absent / malformed
    values normalize to an empty list — never None — so the client never has
    to defend against the field being missing."""

    def test_apt_packages_absent_returns_empty_list(self, client, seed_data):
        # seed_data's command version manifest_json has no apt_packages key.
        resp = client.get("/v1/commands/get_stock_price")
        assert resp.status_code == 200
        data = resp.json()
        assert data["apt_packages"] == []

    def test_apt_packages_surfaced_from_manifest(self, db_session, client, seed_data):
        # Add a newer version with apt_packages declared in the manifest.
        v2 = CommandVersion(
            command_id=1,
            version="1.1.0",
            git_tag="v1.1.0",
            manifest_json={
                "name": "get_stock_price",
                "version": "1.1.0",
                "apt_packages": ["mpv", "ffmpeg"],
            },
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(v2)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price")
        assert resp.status_code == 200
        data = resp.json()
        assert data["apt_packages"] == ["mpv", "ffmpeg"]

    def test_apt_packages_filters_non_string_entries(self, db_session, client, seed_data):
        """Defensive: stored manifests are JSON blobs and could carry garbage.
        Filter to strings rather than 500ing the detail endpoint."""
        v2 = CommandVersion(
            command_id=1,
            version="1.2.0",
            git_tag="v1.2.0",
            manifest_json={
                "name": "get_stock_price",
                "version": "1.2.0",
                "apt_packages": ["mpv", 42, None, "ffmpeg"],
            },
            danger_rating=2,
            min_jarvis_version="0.9.0",
        )
        db_session.add(v2)
        db_session.commit()

        resp = client.get("/v1/commands/get_stock_price")
        assert resp.status_code == 200
        assert resp.json()["apt_packages"] == ["mpv", "ffmpeg"]


class TestListVersions:
    def test_not_found(self, client):
        resp = client.get("/v1/commands/nonexistent/versions")
        assert resp.status_code == 404

    def test_versions_listed(self, client, seed_data):
        resp = client.get("/v1/commands/get_stock_price/versions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["command_name"] == "get_stock_price"
        assert len(data["versions"]) == 1
        assert data["versions"][0]["version"] == "1.0.0"
        assert data["versions"][0]["git_tag"] == "v1.0.0"
