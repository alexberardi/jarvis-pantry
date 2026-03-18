"""Tests for command detail endpoints."""


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
