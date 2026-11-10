"""The Forge's package reading tools: search, list and read published packages (GitHub mocked)."""

from __future__ import annotations

import json

import pytest

from app.models import Command, CommandVersion
from app.services import package_files

TREE = {"tree": [
    {"path": "device_families/zwave", "type": "tree"},
    {"path": "device_families/zwave/protocol.py", "type": "blob", "size": 20},
    {"path": "big.bin", "type": "blob", "size": 300 * 1024},
    {"path": "logo.png", "type": "blob", "size": 4},
], "truncated": False}


@pytest.fixture
def github(monkeypatch):
    """Scripted GitHub: records every URL; returns the call log."""
    package_files.clear_cache()
    calls: list[str] = []

    def fake_get(url, accept, max_bytes):
        calls.append(url)
        if url.startswith("https://api.github.com/repos/testuser/jarvis-zwave/git/trees/abc123"):
            return json.dumps(TREE).encode()
        if url == "https://raw.githubusercontent.com/testuser/jarvis-zwave/abc123/device_families/zwave/protocol.py":
            return b"class ZWave: pass\n"
        if url.endswith("/logo.png"):
            return b"\x89PNG"
        raise package_files.PackageNotFound("Not found on GitHub")

    monkeypatch.setattr(package_files, "_get", fake_get)
    yield calls
    package_files.clear_cache()


@pytest.fixture
def zwave(db_session, seed_data):
    cmd = Command(
        id=3, command_name="zwave", display_name="Z-Wave", description="Z-Wave devices via Z-Wave JS UI",
        github_repo_url="https://github.com/testuser/jarvis-zwave", author_id=1, latest_version="1.2.0",
        package_type="bundle", published=True, install_count=5,
        components=[{"type": "device_protocol", "name": "zwave", "path": "device_families/zwave"}],
    )
    db_session.add(cmd)
    db_session.add(CommandVersion(command_id=3, version="1.2.0", git_tag="v1.2.0", git_commit_sha="abc123"))
    db_session.add(Command(id=4, command_name="hidden", github_repo_url="https://github.com/a/b", author_id=1, published=False))
    db_session.commit()


def test_search_filters_by_query_and_component_type(client, zwave):
    body = client.get("/v1/forge/packages", params={"q": "z-wave"}).json()
    assert [p["name"] for p in body["packages"]] == ["zwave"]
    assert body["packages"][0]["components"][0]["type"] == "device_protocol"
    assert body["packages"][0]["latest_version"] == "1.2.0"

    assert [p["name"] for p in client.get("/v1/forge/packages", params={"type": "device_protocol"}).json()["packages"]] == ["zwave"]
    # Single commands declare no components but still count as commands; unpublished packages never show.
    names = {p["name"] for p in client.get("/v1/forge/packages", params={"type": "command"}).json()["packages"]}
    assert names == {"get_stock_price", "weather_alerts"}
    assert client.get("/v1/forge/packages", params={"type": "nope"}).status_code == 422


def test_list_files_at_the_published_sha(client, zwave, github):
    body = client.get("/v1/forge/packages/zwave/files").json()
    assert body["ref"] == "abc123" and not body["truncated"]
    assert [f["path"] for f in body["files"]] == ["device_families/zwave/protocol.py", "big.bin", "logo.png"]
    client.get("/v1/forge/packages/zwave/files")
    assert len(github) == 1  # cached


def test_read_file(client, zwave, github):
    resp = client.get("/v1/forge/packages/zwave/file", params={"path": "device_families/zwave/protocol.py"})
    assert resp.status_code == 200 and resp.json()["content"] == "class ZWave: pass\n"


@pytest.mark.parametrize("path", ["../etc/passwd", "/etc/passwd", "a//b", "device_families/../x", "a\\b", ""])
def test_read_rejects_unsafe_paths_without_calling_github(client, zwave, github, path):
    assert client.get("/v1/forge/packages/zwave/file", params={"path": path}).status_code == 400
    assert github == []


def test_read_errors(client, zwave, github):
    assert client.get("/v1/forge/packages/zwave/file", params={"path": "missing.py"}).status_code == 404
    assert client.get("/v1/forge/packages/zwave/file", params={"path": "big.bin"}).status_code == 400
    assert client.get("/v1/forge/packages/zwave/file", params={"path": "logo.png"}).status_code == 400
    # Only catalogue packages: unpublished or unknown names never reach GitHub.
    before = len(github)
    assert client.get("/v1/forge/packages/hidden/files").status_code == 404
    assert client.get("/v1/forge/packages/nope/file", params={"path": "a.py"}).status_code == 404
    assert len(github) == before


def test_github_failure_is_502(client, zwave, monkeypatch):
    package_files.clear_cache()

    def boom(url, accept, max_bytes):
        raise package_files.PackageFilesError("GitHub answered HTTP 500")

    monkeypatch.setattr(package_files, "_get", boom)
    assert client.get("/v1/forge/packages/zwave/files").status_code == 502


def test_upstream_repo_url_must_be_plain_github(db_session, monkeypatch):
    from app.services import upstream
    monkeypatch.setattr(upstream, "is_enabled", lambda: True)
    details = {
        "": {"github_repo_url": "https://github.com/x/y?evil"},
        "/versions": {"versions": [{"git_tag": "v1"}]},
    }
    monkeypatch.setattr(upstream, "get_detail", lambda name, suffix="": details[suffix])
    with pytest.raises(package_files.PackageNotFound):
        package_files._locate(db_session, "remote")
    details[""] = {"github_repo_url": "https://github.com/x/y"}
    assert package_files._locate(db_session, "remote") == ("x", "y", "v1")


def test_token_is_sent_to_github_only_as_a_header(monkeypatch):
    from app import config
    monkeypatch.setenv("PANTRY_GITHUB_TOKEN", "secret-token")
    config.get_settings.cache_clear()
    seen = {}

    class Resp:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_bytes(self): yield b"ok"

    def fake_stream(method, url, headers, timeout, follow_redirects):
        seen.update(url=url, headers=headers, follow_redirects=follow_redirects)
        return Resp()

    monkeypatch.setattr(package_files.httpx, "stream", fake_stream)
    try:
        assert package_files._get("https://api.github.com/x", "a", 10) == b"ok"
    finally:
        monkeypatch.delenv("PANTRY_GITHUB_TOKEN")
        config.get_settings.cache_clear()
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in seen["url"] and seen["follow_redirects"] is False


def test_a_published_ref_missing_on_github_reads_the_default_branch(client, zwave, db_session, monkeypatch):
    # zwave's v1.0.2 tag is published in Pantry but not on GitHub: references still read.
    db_session.query(CommandVersion).filter_by(command_id=3).update({"git_commit_sha": None, "git_tag": "v9.9.9"})
    db_session.commit()
    package_files.clear_cache()
    calls: list[str] = []

    def fake_get(url, accept, max_bytes):
        calls.append(url)
        if "/git/trees/HEAD" in url:
            return json.dumps(TREE).encode()
        if url == "https://raw.githubusercontent.com/testuser/jarvis-zwave/HEAD/device_families/zwave/protocol.py":
            return b"class ZWave: pass\n"
        raise package_files.PackageNotFound("Not found on GitHub")

    monkeypatch.setattr(package_files, "_get", fake_get)
    assert client.get("/v1/forge/packages/zwave/files").json()["ref"] == "HEAD"
    body = client.get("/v1/forge/packages/zwave/file", params={"path": "device_families/zwave/protocol.py"}).json()
    assert body["ref"] == "HEAD" and body["content"] == "class ZWave: pass\n"
    package_files.clear_cache()
