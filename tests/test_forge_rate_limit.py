"""POST /v1/forge/generate is unauthenticated and triggers LLM generation —
it carries the tightest per-IP rate limit in the app (P1.1)."""

from types import SimpleNamespace


def _fake_settings(cap: int, disabled: bool = False):
    return SimpleNamespace(
        rate_limit_disabled=disabled,
        forge_generate_rate_limit_per_hour=cap,
        bypass_llm_key=False,
    )


async def _fake_generate(**kwargs):
    return SimpleNamespace(
        package_name="test_pkg",
        display_name="Test Pkg",
        explanation="ok",
        files=[SimpleNamespace(filename="command.py", content="x", language="python")],
        validation=None,
    )


def _post(client):
    return client.post(
        "/v1/forge/generate",
        json={"description": "a weather command", "llm_api_key": "sk-test"},
    )


class TestForgeGenerateRateLimit:
    def test_429_after_cap(self, client, monkeypatch):
        monkeypatch.setattr("app.config.get_settings", lambda: _fake_settings(cap=2))
        monkeypatch.setattr(
            "app.services.forge_generator.generate_package", _fake_generate
        )

        assert _post(client).status_code == 200
        assert _post(client).status_code == 200
        resp = _post(client)
        assert resp.status_code == 429
        assert "Rate limit" in resp.json()["detail"]

    def test_limit_checked_before_generation_runs(self, client, monkeypatch):
        calls = {"n": 0}

        async def _generate(**kwargs):
            calls["n"] += 1
            return await _fake_generate()

        monkeypatch.setattr("app.config.get_settings", lambda: _fake_settings(cap=0))
        monkeypatch.setattr(
            "app.services.forge_generator.generate_package", _generate
        )

        assert _post(client).status_code == 429
        assert calls["n"] == 0

    def test_rate_limit_disabled_bypasses(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.config.get_settings", lambda: _fake_settings(cap=0, disabled=True)
        )
        monkeypatch.setattr(
            "app.services.forge_generator.generate_package", _fake_generate
        )

        assert _post(client).status_code == 200
