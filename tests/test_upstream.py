"""Upstream catalogue mirror and adding private packages from a repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import upstream

UP = "https://public.example"

UPSTREAM_ITEMS = [
    {"command_name": "spotify", "display_name": "Spotify", "description": "Music from Spotify", "categories": ["media"],
     "install_count": 10, "package_type": "bundle", "latest_version": "2.1.7"},
    {"command_name": "get_weather_meteo", "display_name": "Weather", "description": "Open-Meteo forecasts",
     "categories": ["weather"], "install_count": 50, "package_type": "command", "latest_version": "1.0.0"},
    # Same name as a seeded local package: the local one must win.
    {"command_name": "weather_alerts", "display_name": "Upstream Weather Alerts", "description": "x",
     "categories": ["weather"], "install_count": 999, "package_type": "command", "latest_version": "9.9.9"},
]


@pytest.fixture
def mirror(monkeypatch):
    """Enable the upstream mirror with a scripted upstream; returns the call log."""
    from app import config
    monkeypatch.setenv("PANTRY_UPSTREAM_URL", UP)
    config.get_settings.cache_clear()
    upstream.clear_cache()
    calls: list[tuple[str, dict | None]] = []
    state = {"fail": False}

    def fake_get_json(path, params=None):
        calls.append((path, params))
        if state["fail"]:
            raise upstream.UpstreamError("boom")
        if path == "/v1/commands":
            return 200, {"commands": [dict(i) for i in UPSTREAM_ITEMS], "total": len(UPSTREAM_ITEMS), "page": 1, "per_page": 100}
        if path == "/v1/commands/spotify":
            return 200, {"command_name": "spotify", "latest_version": "2.1.7", "github_repo_url": "https://github.com/x/spotify"}
        if path == "/v1/commands/spotify/versions":
            return 200, {"command_name": "spotify", "versions": [{"version": "2.1.7"}]}
        if path == "/v1/commands/spotify/download":
            return 200, {"command_name": "spotify", "github_repo_url": "https://github.com/x/spotify", "version": "2.1.7"}
        return 404, {"detail": "not found"}

    monkeypatch.setattr(upstream, "_get_json", fake_get_json)
    yield type("M", (), {"calls": calls, "state": state})
    monkeypatch.delenv("PANTRY_UPSTREAM_URL", raising=False)
    config.get_settings.cache_clear()
    upstream.clear_cache()


def _names(resp):
    return [c["command_name"] for c in resp.json()["commands"]]


def test_disabled_listing_is_local_only_and_unmarked(client, seed_data):
    body = client.get("/v1/commands").json()
    assert body["total"] == 2
    assert all("source" not in c for c in body["commands"])


def test_merged_listing_local_first_and_deduplicated(client, seed_data, mirror):
    r = client.get("/v1/commands")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4  # 2 local + 2 upstream; the upstream weather_alerts is shadowed
    names = _names(r)
    assert set(names[:2]) == {"get_stock_price", "weather_alerts"}
    assert set(names[2:]) == {"get_weather_meteo", "spotify"}
    sources = {c["command_name"]: c["source"] for c in body["commands"]}
    assert sources["weather_alerts"] == "local" and sources["spotify"] == "upstream"
    local_alerts = next(c for c in body["commands"] if c["command_name"] == "weather_alerts")
    assert local_alerts["latest_version"] == "2.1.0"


def test_merged_filters_and_pagination(client, seed_data, mirror):
    assert _names(client.get("/v1/commands", params={"q": "spot"})) == ["spotify"]
    assert set(_names(client.get("/v1/commands", params={"category": "weather"}))) == {"weather_alerts", "get_weather_meteo"}
    page2 = client.get("/v1/commands", params={"per_page": 3, "page": 2}).json()
    assert page2["total"] == 4 and len(page2["commands"]) == 1


def test_categories_include_upstream(client, seed_data, mirror):
    cats = {c["name"]: c["count"] for c in client.get("/v1/categories").json()["categories"]}
    assert cats["media"] == 1 and cats["weather"] == 2  # local weather_alerts + upstream get_weather_meteo


def test_detail_falls_back_to_upstream_and_local_wins(client, seed_data, mirror):
    r = client.get("/v1/commands/spotify")
    assert r.status_code == 200 and r.json()["source"] == "upstream"
    local = client.get("/v1/commands/weather_alerts").json()
    assert local["source"] == "local" and local["latest_version"] == "2.1.0"
    assert client.get("/v1/commands/nonexistent").status_code == 404
    assert client.get("/v1/commands/spotify/versions").json()["versions"][0]["version"] == "2.1.7"


def test_download_passthrough_and_local(client, seed_data, mirror):
    r = client.get("/v1/commands/spotify/download")
    assert r.status_code == 200 and r.json()["github_repo_url"] == "https://github.com/x/spotify"
    assert ("/v1/commands/spotify/download", None) in mirror.calls
    local = client.get("/v1/commands/get_stock_price/download")
    assert local.status_code == 200 and local.json()["version"] == "1.0.0"
    assert client.get("/v1/commands/nonexistent/download").status_code == 404


def test_download_upstream_unreachable_is_502(client, seed_data, mirror):
    mirror.state["fail"] = True
    assert client.get("/v1/commands/spotify/download").status_code == 502


def test_listing_survives_an_offline_upstream(client, seed_data, mirror):
    mirror.state["fail"] = True
    body = client.get("/v1/commands").json()
    assert body["total"] == 2 and set(_names(client.get("/v1/commands"))) == {"get_stock_price", "weather_alerts"}


def test_upstream_list_is_cached(client, seed_data, mirror):
    client.get("/v1/commands"); client.get("/v1/commands"); client.get("/v1/categories")
    assert [c for c in mirror.calls if c[0] == "/v1/commands"].__len__() == 1


# ── add from repository ───────────────────────────────────────────────

def _repo(tmp_path: Path, name="my_private") -> Path:
    root = tmp_path / "clone" / "repo"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# x\n")
    (root / "LICENSE").write_text("MIT\n")
    (root / "command.py").write_text("# command\n")
    (root / "jarvis_package.yaml").write_text(
        f"name: {name}\ndescription: A private thing\nversion: 0.3.0\ndisplay_name: My Private\n"
    )
    return root


@pytest.fixture
def admin(monkeypatch):
    from app import config
    monkeypatch.setenv("ADMIN_API_KEY", "adm")
    config.get_settings.cache_clear()
    yield {"X-Admin-Key": "adm"}
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    config.get_settings.cache_clear()


def test_add_from_repo_refuses_without_an_admin_key_configured(client):
    r = client.post("/v1/admin/commands/from-repo", json={"github_repo_url": "https://github.com/me/x"})
    assert r.status_code == 403 and "ADMIN_API_KEY" in r.json()["detail"]


def test_add_from_repo_needs_the_key(client, admin):
    r = client.post("/v1/admin/commands/from-repo", json={"github_repo_url": "https://github.com/me/x"})
    assert r.status_code == 401


def test_add_from_repo_publishes_and_lists_as_local(client, admin, mirror, tmp_path, monkeypatch):
    seen = {}

    def fake_clone(url, ref=None):
        seen["url"], seen["ref"] = url, ref
        return _repo(tmp_path), "deadbeef" * 5

    monkeypatch.setattr("app.api.local_commands.clone_repo", fake_clone)
    r = client.post(
        "/v1/admin/commands/from-repo",
        json={"github_repo_url": "https://github.com/me/jarvis-cmd-private/", "ref": "v0.3.0", "token": "ghp_secrettoken"},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["command_name"] == "my_private" and body["version"] == "0.3.0" and body["private_repository"] is True
    assert "ghp_secrettoken" not in r.text
    assert seen["url"].startswith("https://x-access-token:ghp_secrettoken@github.com/") and seen["ref"] == "v0.3.0"

    detail = client.get("/v1/commands/my_private").json()
    assert detail["source"] == "local" and detail["github_repo_url"] == "https://github.com/me/jarvis-cmd-private"
    assert "ghp_secrettoken" not in str(detail)
    dl = client.get("/v1/commands/my_private/download").json()
    assert dl["version"] == "0.3.0" and dl["git_commit_sha"] == "deadbeef" * 5
    assert "my_private" in _names(client.get("/v1/commands"))

    # Unpublish
    assert client.delete("/v1/admin/commands/my_private/local", headers=admin).status_code == 200
    assert client.get("/v1/commands/my_private").status_code == 404


def test_add_from_repo_rejects_non_https(client, admin):
    r = client.post("/v1/admin/commands/from-repo", json={"github_repo_url": "git@github.com:me/x.git"}, headers=admin)
    assert r.status_code == 400


def test_add_from_repo_refuses_to_replace_a_package_that_is_not_local(client, seed_data, admin, tmp_path, monkeypatch):
    """A reviewed public package must not be repointed at an administrator's repository."""
    from app.models import Command

    def fake_clone(url, ref=None):
        # The manifest names the existing public package.
        return _repo(tmp_path, name="get_stock_price"), "cafe" * 10

    monkeypatch.setattr("app.api.local_commands.clone_repo", fake_clone)
    r = client.post("/v1/admin/commands/from-repo",
                    json={"github_repo_url": "https://github.com/me/evil-stock"}, headers=admin)
    assert r.status_code == 409 and "not replaced" in r.json()["detail"]
    row = client.get("/v1/commands/get_stock_price").json()
    assert row["github_repo_url"] == "https://github.com/testuser/jarvis-command-stock-price"
    assert row["author"]["github"] == "testuser"


def test_add_from_repo_can_update_a_package_it_added(client, admin, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.local_commands.clone_repo", lambda url, ref=None: (_repo(tmp_path), "ab" * 20))
    body = {"github_repo_url": "https://github.com/me/jarvis-cmd-private"}
    assert client.post("/v1/admin/commands/from-repo", json=body, headers=admin).status_code == 200
    again = client.post("/v1/admin/commands/from-repo", json=body, headers=admin)
    assert again.status_code == 200 and again.json()["command_name"] == "my_private"


def test_upstream_path_segments_are_quoted(monkeypatch):
    from app.services import upstream

    seen = []
    monkeypatch.setenv("PANTRY_UPSTREAM_URL", "https://public.example")
    from app import config
    config.get_settings.cache_clear()
    upstream.clear_cache()
    monkeypatch.setattr(upstream, "_get_json", lambda path, params=None: (seen.append(path), (404, None))[1])
    upstream.get_detail("../admin/secret")
    upstream.get_download("a/b?c=d", None)
    assert seen == ["/v1/commands/..%2Fadmin%2Fsecret", "/v1/commands/a%2Fb%3Fc%3Dd/download"]
    monkeypatch.delenv("PANTRY_UPSTREAM_URL")
    config.get_settings.cache_clear()
    upstream.clear_cache()


def test_local_llm_error_body_is_not_echoed(monkeypatch):
    import asyncio

    import httpx

    from app.services import llm_client

    class Resp:
        status_code = 500
        text = "internal detail with sk-SECRET-token"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(llm_client.call_local("sys", [], "k", "m", 10, "http://srv"))
    assert "sk-SECRET" not in str(exc.value) and "HTTP 500" in str(exc.value)
