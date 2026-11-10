"""The per-IP limits key on the caller's real address. On Fly, X-Forwarded-For is whatever the
caller sent (uvicorn trusts every proxy there), so the limits use Fly-Client-IP instead."""

from types import SimpleNamespace

from app.api import forge_drafts
from app.rate_limiter import client_ip


def _settings(trust: bool):
    return SimpleNamespace(trust_fly_client_ip=trust, rate_limit_disabled=False)


def _request(headers: dict, host="10.0.0.9"):
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()}, client=SimpleNamespace(host=host))


def test_fly_header_is_the_address_behind_fly(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(True))
    assert client_ip(_request({"Fly-Client-IP": "203.0.113.7"})) == "203.0.113.7"


def test_fly_header_is_ignored_without_fly(monkeypatch):
    # Not behind Fly, anyone could send the header: the socket's address counts.
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(False))
    assert client_ip(_request({"Fly-Client-IP": "203.0.113.7"})) == "10.0.0.9"


def test_spoofed_forwarded_for_shares_the_callers_bucket(client, monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: _settings(True))
    monkeypatch.setattr(forge_drafts._feedback_limiter, "requests_per_hour", 2)
    statuses = [
        client.post(
            "/v1/forge/drafts/ZZZZZZ/feedback",
            json={"install": {}, "runs": []},
            headers={"Fly-Client-IP": "203.0.113.7", "X-Forwarded-For": f"198.51.100.{i}"},
        ).status_code
        for i in range(3)
    ]
    assert statuses == [404, 404, 429]
