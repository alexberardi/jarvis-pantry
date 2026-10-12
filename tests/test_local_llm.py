"""Local OpenAI-compatible server settings, model detection and generation through it."""

from __future__ import annotations

import httpx
import pytest

from app.services import local_llm


def _fake_get(models, status=200, seen=None):
    def fake(url, headers=None, timeout=None):
        if seen is not None:
            seen.append((url, headers))
        request = httpx.Request("GET", url)
        if status != 200:
            return httpx.Response(status, request=request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": m} for m in models]}, request=request)
    return fake


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    local_llm._models_cache.clear()
    for k in ("FORGE_LOCAL_BASE_URL", "FORGE_LOCAL_API_KEY", "FORGE_LOCAL_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield
    local_llm._models_cache.clear()


def test_normalise_base_url():
    assert local_llm.normalise_base_url(" http://10.0.0.5:8080/v1/ ") == "http://10.0.0.5:8080"
    assert local_llm.normalise_base_url("https://llm.lan") == "https://llm.lan"
    assert local_llm.normalise_base_url("") == ""
    with pytest.raises(ValueError):
        local_llm.normalise_base_url("ftp://host")


def test_detect_models_sorted_and_sends_key(monkeypatch):
    seen = []
    monkeypatch.setattr(httpx, "get", _fake_get(["b-model", "A-model", "b-model"], seen=seen))
    assert local_llm.detect_models("http://srv:8080/v1", "sekret") == ["A-model", "b-model"]
    assert seen[0][0] == "http://srv:8080/v1/models"
    assert seen[0][1] == {"Authorization": "Bearer sekret"}


def test_detect_models_errors(monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get([], status=401))
    with pytest.raises(local_llm.LocalLLMError, match="rejected the API key"):
        local_llm.detect_models("http://srv:8080")
    monkeypatch.setattr(httpx, "get", _fake_get([], status=500))
    with pytest.raises(local_llm.LocalLLMError, match="HTTP 500"):
        local_llm.detect_models("http://srv:8080")


def test_settings_roundtrip_never_returns_key(client, monkeypatch):
    r = client.get("/v1/forge/local-llm")
    assert r.status_code == 200 and r.json()["base_url"] == "" and r.json()["source"] == "none"

    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080/v1", "api_key": "k", "default_model": "m1"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"base_url": "http://srv:8080", "has_api_key": True, "default_model": "m1", "source": "database"}
    assert "api_key" not in body

    # api_key omitted keeps the stored key; "" clears it
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "default_model": "m2"})
    assert r.json()["has_api_key"] is True and r.json()["default_model"] == "m2"
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "api_key": ""})
    assert r.json()["has_api_key"] is False


def test_settings_reject_bad_url(client):
    r = client.put("/v1/forge/local-llm", json={"base_url": "not a url"})
    assert r.status_code == 400


def test_admin_key_protects_changes_when_set(client, monkeypatch):
    from app import config
    monkeypatch.setenv("ADMIN_API_KEY", "adm")
    config.get_settings.cache_clear()
    try:
        assert client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080"}).status_code == 401
        ok = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080"}, headers={"X-Admin-Key": "adm"})
        assert ok.status_code == 200
        assert client.post("/v1/forge/local-llm/detect").status_code == 401
    finally:
        monkeypatch.delenv("ADMIN_API_KEY")
        config.get_settings.cache_clear()


def test_detect_endpoint_and_model_list(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b", "coder-80b"]))
    r = client.post("/v1/forge/local-llm/detect", json={"base_url": "http://srv:8080"})
    assert r.json() == {"ok": True, "models": ["coder-80b", "qwen-9b"], "default_model": ""}

    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "default_model": "qwen-9b"})
    models = client.get("/v1/forge/models").json()["models"]
    assert [m["id"] for m in models[:2]] == ["local:qwen-9b", "local:coder-80b"]
    assert models[0]["default"] is True and models[0]["estimated_cost_usd"] == 0.0
    assert any(m["id"] == "sonnet" for m in models)


def test_detect_reports_failure_without_raising(client, monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    r = client.post("/v1/forge/local-llm/detect", json={"base_url": "http://srv:8080"})
    assert r.status_code == 200 and r.json()["ok"] is False and "refused" in r.json()["error"]


def test_models_list_without_local_server_is_unchanged(client):
    ids = [m["id"] for m in client.get("/v1/forge/models").json()["models"]]
    assert ids[0] == "haiku" and not any(i.startswith("local") for i in ids)


def test_env_fallback(client, monkeypatch):
    monkeypatch.setenv("FORGE_LOCAL_BASE_URL", "http://env-srv:8080/")
    monkeypatch.setenv("FORGE_LOCAL_MODEL", "env-model")
    r = client.get("/v1/forge/local-llm").json()
    assert r["base_url"] == "http://env-srv:8080" and r["default_model"] == "env-model" and r["source"] == "environment"


def test_generate_uses_selected_local_model_without_key(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b"]))
    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "api_key": "srvkey"})
    captured = {}

    async def fake_call_local(system_prompt, messages, api_key, model, max_tokens, base_url):
        captured.update(api_key=api_key, model=model, base_url=base_url)
        return '{"package_name": "x", "display_name": "X", "explanation": "ok", "files": []}'

    from app.services import forge_generator
    monkeypatch.setattr(forge_generator, "call_local", fake_call_local)
    r = client.post("/v1/forge/generate", json={"description": "do a thing", "model": "local:qwen-9b", "llm_api_key": ""})
    assert r.status_code == 200, r.text
    assert captured == {"api_key": "srvkey", "model": "qwen-9b", "base_url": "http://srv:8080"}


def test_generate_unknown_local_model_when_unconfigured(client):
    r = client.post("/v1/forge/generate", json={"description": "x", "model": "local:nothing", "llm_api_key": ""})
    assert r.status_code == 400 and "Unknown model" in r.json()["detail"]
