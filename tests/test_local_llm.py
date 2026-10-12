"""Local OpenAI-compatible server settings, model detection and generation through it."""

from __future__ import annotations

import httpx
import pytest

from app.services import local_llm


ADMIN = {"X-Admin-Key": "adm"}


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
    from app import config
    monkeypatch.setenv("FORGE_LOCAL_LLM_ENABLED", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "adm")
    config.get_settings.cache_clear()
    for k in ("FORGE_LOCAL_BASE_URL", "FORGE_LOCAL_API_KEY", "FORGE_LOCAL_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield
    monkeypatch.delenv("FORGE_LOCAL_LLM_ENABLED", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    config.get_settings.cache_clear()


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

    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080/v1", "api_key": "k", "default_model": "m1"}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body == {"base_url": "http://srv:8080", "has_api_key": True, "default_model": "m1", "source": "database"}
    assert "api_key" not in body

    # api_key omitted keeps the stored key; "" clears it
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "default_model": "m2"}, headers=ADMIN)
    assert r.json()["has_api_key"] is True and r.json()["default_model"] == "m2"
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "api_key": ""}, headers=ADMIN)
    assert r.json()["has_api_key"] is False


def test_settings_reject_bad_url(client):
    r = client.put("/v1/forge/local-llm", json={"base_url": "not a url"}, headers=ADMIN)
    assert r.status_code == 400


def test_changes_and_probes_always_need_the_admin_key(client, monkeypatch):
    from app import config

    body = {"base_url": "http://srv:8080"}
    # Wrong or missing key: 401.
    assert client.put("/v1/forge/local-llm", json=body).status_code == 401
    assert client.put("/v1/forge/local-llm", json=body, headers={"X-Admin-Key": "nope"}).status_code == 401
    assert client.post("/v1/forge/local-llm/detect", json=body).status_code == 401
    assert client.put("/v1/forge/local-llm", json=body, headers=ADMIN).status_code == 200

    # No key configured on the Pantry at all: refused (403), never open.
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    config.get_settings.cache_clear()
    r = client.put("/v1/forge/local-llm", json=body, headers=ADMIN)
    assert r.status_code == 403 and "ADMIN_API_KEY" in r.json()["detail"]
    assert client.post("/v1/forge/local-llm/detect", json=body, headers=ADMIN).status_code == 403


def test_saved_key_is_only_sent_to_the_saved_server(client, monkeypatch):
    seen = []
    monkeypatch.setattr(httpx, "get", _fake_get(["m"], seen=seen))
    client.put("/v1/forge/local-llm", json={"base_url": "http://trusted:8080", "api_key": "SAVEDKEY"}, headers=ADMIN)
    seen.clear()

    # Another address: the saved key must NOT go along.
    client.post("/v1/forge/local-llm/detect", json={"base_url": "http://elsewhere:9000"}, headers=ADMIN)
    assert seen[-1][0] == "http://elsewhere:9000/v1/models" and seen[-1][1] == {}

    # The caller's own key is used for another address.
    client.post("/v1/forge/local-llm/detect", json={"base_url": "http://elsewhere:9000", "api_key": "THEIRS"}, headers=ADMIN)
    assert seen[-1][1] == {"Authorization": "Bearer THEIRS"}

    # The saved address (or none given): the saved key is used.
    client.post("/v1/forge/local-llm/detect", json={}, headers=ADMIN)
    assert seen[-1][0] == "http://trusted:8080/v1/models" and seen[-1][1] == {"Authorization": "Bearer SAVEDKEY"}
    client.post("/v1/forge/local-llm/detect", json={"base_url": "http://trusted:8080/v1/"}, headers=ADMIN)
    assert seen[-1][1] == {"Authorization": "Bearer SAVEDKEY"}


def test_public_model_list_never_calls_the_saved_server(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b", "coder-80b"]))
    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "default_model": "qwen-9b"}, headers=ADMIN)

    def forbidden(*a, **k):
        raise AssertionError("an unauthenticated request made the Pantry call the saved server")

    monkeypatch.setattr(httpx, "get", forbidden)
    ids = [m["id"] for m in client.get("/v1/forge/models").json()["models"]]
    assert ids[:2] == ["local:qwen-9b", "local:coder-80b"]


def test_detect_endpoint_and_model_list(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b", "coder-80b"]))
    # A probe of an address that is not the saved server returns models but stores nothing.
    r = client.post("/v1/forge/local-llm/detect", json={"base_url": "http://srv:8080"}, headers=ADMIN)
    assert r.json() == {"ok": True, "models": ["coder-80b", "qwen-9b"], "default_model": ""}
    assert not any(m["id"].startswith("local:") for m in client.get("/v1/forge/models").json()["models"])

    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "default_model": "qwen-9b"}, headers=ADMIN)
    models = client.get("/v1/forge/models").json()["models"]
    assert [m["id"] for m in models[:2]] == ["local:qwen-9b", "local:coder-80b"]
    assert models[0]["default"] is True and models[0]["estimated_cost_usd"] == 0.0
    assert any(m["id"] == "sonnet" for m in models)


def test_detect_reports_failure_without_raising(client, monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    r = client.post("/v1/forge/local-llm/detect", json={"base_url": "http://srv:8080"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["ok"] is False and "refused" in r.json()["error"]


def test_models_list_without_local_server_is_unchanged(client):
    ids = [m["id"] for m in client.get("/v1/forge/models").json()["models"]]
    assert ids[0] == "haiku" and not any(i.startswith("local") for i in ids)


def test_env_fallback(client, monkeypatch):
    monkeypatch.setenv("FORGE_LOCAL_BASE_URL", "http://env-srv:8080/")
    monkeypatch.setenv("FORGE_LOCAL_MODEL", "env-model")
    r = client.get("/v1/forge/local-llm", headers=ADMIN).json()
    assert r["base_url"] == "http://env-srv:8080" and r["default_model"] == "env-model" and r["source"] == "environment"


def test_server_address_is_only_shown_with_the_admin_key(client, monkeypatch):
    monkeypatch.setenv("FORGE_LOCAL_BASE_URL", "http://internal-srv:8080")
    anonymous = client.get("/v1/forge/local-llm").json()
    assert anonymous["base_url"] == "" and "internal-srv" not in str(anonymous)
    wrong = client.get("/v1/forge/local-llm", headers={"X-Admin-Key": "nope"}).json()
    assert wrong["base_url"] == ""
    assert client.get("/v1/forge/local-llm", headers=ADMIN).json()["base_url"] == "http://internal-srv:8080"


def test_a_key_supplied_by_the_caller_never_reaches_the_local_server(client, monkeypatch):
    pytest.importorskip("jarvis_command_sdk")  # forge_generator imports the SDK, which is not in the dev extras
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b"]))
    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "api_key": "srvkey"}, headers=ADMIN)
    seen = {}

    async def fake_call_local(system_prompt, messages, api_key, model, max_tokens, base_url):
        seen["api_key"] = api_key
        return '{"package_name": "x", "display_name": "X", "explanation": "ok", "files": []}'

    from app.services import forge_generator
    monkeypatch.setattr(forge_generator, "call_local", fake_call_local)
    r = client.post("/v1/forge/generate",
                    json={"description": "do a thing", "model": "local:qwen-9b", "llm_api_key": "sk-hosted-provider"})
    assert r.status_code == 200, r.text
    assert seen["api_key"] == "srvkey"


def test_generate_uses_selected_local_model_without_key(client, monkeypatch):
    pytest.importorskip("jarvis_command_sdk")  # the validator behind /generate needs the SDK, which is not in the dev extras
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b"]))
    client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080", "api_key": "srvkey"}, headers=ADMIN)
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


def test_features_reports_enabled(client):
    f = client.get("/v1/forge/features").json()
    assert f["local_llm"] is True and f["upstream_mirror"] is False
    # The autouse fixture configures an admin key.
    assert f["private_packages"] is True and f["admin_key_configured"] is True


def test_everything_is_hidden_when_disabled(client, monkeypatch):
    from app import config
    monkeypatch.setenv("FORGE_LOCAL_LLM_ENABLED", "false")
    monkeypatch.setenv("FORGE_LOCAL_BASE_URL", "http://env-srv:8080")  # must be ignored while disabled
    config.get_settings.cache_clear()
    monkeypatch.setattr(httpx, "get", _fake_get(["qwen-9b"]))

    assert client.get("/v1/forge/features").json()["local_llm"] is False
    assert client.get("/v1/forge/local-llm").status_code == 404
    assert client.put("/v1/forge/local-llm", json={"base_url": "http://srv:8080"}, headers=ADMIN).status_code == 404
    assert client.post("/v1/forge/local-llm/detect", json={"base_url": "http://srv:8080"}, headers=ADMIN).status_code == 404

    ids = [m["id"] for m in client.get("/v1/forge/models").json()["models"]]
    assert ids[0] == "haiku" and not any(i.startswith("local") for i in ids)

    r = client.post("/v1/forge/generate", json={"description": "x", "model": "local:qwen-9b", "llm_api_key": ""})
    assert r.status_code == 400 and "Unknown model" in r.json()["detail"]


def test_disabled_by_default(client, monkeypatch):
    from app import config
    monkeypatch.delenv("FORGE_LOCAL_LLM_ENABLED", raising=False)
    config.get_settings.cache_clear()
    assert client.get("/v1/forge/features").json()["local_llm"] is False


def test_cors_preflight_allows_put_from_the_web_interface(client):
    # The web interface is on another origin (port) than the API, so saving settings is a cross-origin PUT.
    r = client.options(
        "/v1/forge/local-llm",
        headers={
            "Origin": "http://10.0.0.20:7720",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type,x-admin-key",
        },
    )
    assert r.status_code == 200
    assert "PUT" in r.headers["access-control-allow-methods"]


def test_saving_only_the_address_keeps_the_environment_key_and_model(client, monkeypatch):
    # First save from the web page sends no key: the key from the environment must keep working.
    monkeypatch.setenv("FORGE_LOCAL_BASE_URL", "http://env-srv:8080")
    monkeypatch.setenv("FORGE_LOCAL_API_KEY", "envkey")
    monkeypatch.setenv("FORGE_LOCAL_MODEL", "env-model")
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://other:8080"}, headers=ADMIN)
    assert r.json()["has_api_key"] is True and r.json()["default_model"] == "env-model"
    assert r.json()["source"] == "database"
    # Clearing it on purpose is respected.
    r = client.put("/v1/forge/local-llm", json={"base_url": "http://other:8080", "api_key": ""}, headers=ADMIN)
    assert r.json()["has_api_key"] is False


def test_features_report_private_packages_and_mirror(client, monkeypatch):
    from app import config
    monkeypatch.setenv("ADMIN_API_KEY", "adm")
    monkeypatch.setenv("PANTRY_UPSTREAM_URL", "https://public.example")
    config.get_settings.cache_clear()
    try:
        f = client.get("/v1/forge/features").json()
        assert f["private_packages"] is True and f["upstream_mirror"] is True
    finally:
        monkeypatch.delenv("ADMIN_API_KEY"); monkeypatch.delenv("PANTRY_UPSTREAM_URL")
        config.get_settings.cache_clear()
