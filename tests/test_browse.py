"""Tests for browse/search endpoints."""


class TestListCommands:
    def test_empty_catalog(self, client):
        resp = client.get("/v1/commands")
        assert resp.status_code == 200
        data = resp.json()
        assert data["commands"] == []
        assert data["total"] == 0

    def test_list_published(self, client, seed_data):
        resp = client.get("/v1/commands")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {c["command_name"] for c in data["commands"]}
        assert "get_stock_price" in names
        assert "weather_alerts" in names

    def test_search_by_query(self, client, seed_data):
        resp = client.get("/v1/commands?q=stock")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["commands"][0]["command_name"] == "get_stock_price"

    def test_filter_by_category(self, client, seed_data):
        resp = client.get("/v1/commands?category=finance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["commands"][0]["command_name"] == "get_stock_price"

    def test_sort_by_name(self, client, seed_data):
        resp = client.get("/v1/commands?sort=name")
        assert resp.status_code == 200
        names = [c["command_name"] for c in resp.json()["commands"]]
        assert names == sorted(names)

    def test_pagination(self, client, seed_data):
        resp = client.get("/v1/commands?per_page=1&page=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["commands"]) == 1
        assert data["total"] == 2


class TestListCategories:
    def test_empty(self, client):
        resp = client.get("/v1/categories")
        assert resp.status_code == 200
        assert resp.json()["categories"] == []

    def test_with_data(self, client, seed_data):
        resp = client.get("/v1/categories")
        assert resp.status_code == 200
        categories = {c["name"]: c["count"] for c in resp.json()["categories"]}
        assert categories["finance"] == 1
        assert categories["weather"] == 1
