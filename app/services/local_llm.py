"""Local OpenAI-compatible LLM server (llama-swap, llama.cpp, vLLM, Ollama) used by the Forge.

Settings live in the ``app_settings`` table so they can be changed from the web interface. Environment variables
(``FORGE_LOCAL_BASE_URL``, ``FORGE_LOCAL_API_KEY``, ``FORGE_LOCAL_MODEL``) are the fallback, so a deployment that only
sets them behaves as before. The model list comes from the server's ``/v1/models`` endpoint.

A Jarvis server (jarvisd's LLM, ``http://<jarvis>:7700/llm``) is set up with a Jarvis app ID: it takes its app
credentials as ``X-Jarvis-App-Id``/``X-Jarvis-App-Key`` headers, not a Bearer key, and serves roles, not model files:
``background`` and ``live`` (any other model name would run on the live model, the one voice uses).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..models import AppSetting

KEY_BASE_URL = "local_llm.base_url"
KEY_API_KEY = "local_llm.api_key"
KEY_DEFAULT_MODEL = "local_llm.default_model"
KEY_MODELS = "local_llm.models"
KEY_JARVIS_APP_ID = "local_llm.jarvis_app_id"

# What a Jarvis server offers the Forge: its background model first (a long generation there never holds up voice).
JARVIS_MODELS = ["background", "live"]



class LocalLLMError(RuntimeError):
    """The server could not be reached or did not return a model list."""


@dataclass
class LocalLLMConfig:
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    jarvis_app_id: str = ""  # set: the server is Jarvis, api_key is that app's key
    source: str = "none"  # "database", "environment" or "none"

    @property
    def configured(self) -> bool:
        return bool(self.base_url)


def is_enabled() -> bool:
    """Master switch (FORGE_LOCAL_LLM_ENABLED). When off, none of the local LLM features are reachable or visible."""
    from ..config import get_settings
    return bool(getattr(get_settings(), "forge_local_llm_enabled", False))


def normalise_base_url(url: str) -> str:
    """Strip whitespace, a trailing slash and a trailing /v1, and require http(s)."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Server URL must start with http:// or https://")
    return url


def _get(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row is not None else None


def _set(db: Session, key: str, value: str | None) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def get_config(db: Session) -> LocalLLMConfig:
    """Database values win; environment variables fill anything the database does not hold."""
    if not is_enabled():
        return LocalLLMConfig()
    base = _get(db, KEY_BASE_URL)
    key = _get(db, KEY_API_KEY)
    model = _get(db, KEY_DEFAULT_MODEL)
    if base is not None:
        # A value never saved (None) falls back to the environment; one saved as "" was cleared on purpose.
        if key is None:
            key = os.environ.get("FORGE_LOCAL_API_KEY", "")
        if model is None:
            model = os.environ.get("FORGE_LOCAL_MODEL", "")
        app_id = _get(db, KEY_JARVIS_APP_ID)
        if app_id is None:
            app_id = os.environ.get("FORGE_LOCAL_JARVIS_APP_ID", "")
        return LocalLLMConfig(base_url=base, api_key=key, default_model=model, jarvis_app_id=app_id, source="database")
    env_base = os.environ.get("FORGE_LOCAL_BASE_URL", "").strip().rstrip("/")
    if env_base:
        return LocalLLMConfig(
            base_url=env_base,
            api_key=os.environ.get("FORGE_LOCAL_API_KEY", ""),
            default_model=os.environ.get("FORGE_LOCAL_MODEL", ""),
            jarvis_app_id=os.environ.get("FORGE_LOCAL_JARVIS_APP_ID", ""),
            source="environment",
        )
    return LocalLLMConfig()


def save_config(
    db: Session, base_url: str, api_key: str | None, default_model: str | None, jarvis_app_id: str | None = None,
) -> LocalLLMConfig:
    """Store settings. ``api_key`` None keeps the stored key, an empty string clears it; ``jarvis_app_id`` likewise
    (an empty string: not a Jarvis server)."""
    base_url = normalise_base_url(base_url)
    _set(db, KEY_BASE_URL, base_url)
    if jarvis_app_id is not None:
        _set(db, KEY_JARVIS_APP_ID, jarvis_app_id.strip())
    if api_key is not None:
        _set(db, KEY_API_KEY, api_key.strip())
    if default_model is not None:
        _set(db, KEY_DEFAULT_MODEL, default_model.strip())
    db.commit()
    return get_config(db)


def auth_headers(api_key: str, jarvis_app_id: str = "") -> dict[str, str]:
    """A Jarvis server's app credentials, or an OpenAI-style Bearer key (none without a key)."""
    if jarvis_app_id:
        return {"X-Jarvis-App-Id": jarvis_app_id, "X-Jarvis-App-Key": api_key}
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def detect_models(base_url: str, api_key: str = "", timeout: float = 8.0, jarvis_app_id: str = "") -> list[str]:
    """Ask an OpenAI-compatible server which models it offers (GET /v1/models). A Jarvis server answers with its
    model files, but the Forge asks it for a role: once it answers, its models are JARVIS_MODELS."""
    base_url = normalise_base_url(base_url)
    if not base_url:
        raise LocalLLMError("No server URL set")
    if jarvis_app_id and not api_key:
        raise LocalLLMError("A Jarvis server needs the app's key as well as its ID")
    headers = auth_headers(api_key, jarvis_app_id)
    try:
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise LocalLLMError(f"Could not reach {base_url}: {e}") from e
    if resp.status_code in (401, 403):
        raise LocalLLMError("The server rejected the API key (HTTP %d)" % resp.status_code)
    if resp.status_code != 200:
        raise LocalLLMError(f"The server answered HTTP {resp.status_code} for /v1/models")
    try:
        data: Any = resp.json()
        ids = [m["id"] for m in data["data"] if isinstance(m, dict) and m.get("id")]
    except (ValueError, KeyError, TypeError) as e:
        raise LocalLLMError("The server did not return an OpenAI-style model list") from e
    if jarvis_app_id:
        # Its /v1/models needs no credentials, so a wrong app key only shows at the first generation.
        return list(JARVIS_MODELS)
    return sorted(set(ids), key=str.lower)


def get_stored_models(db: Session) -> list[str]:
    """Model ids from the last successful detection (an administrator action). Never calls the server.

    The public ``GET /v1/forge/models`` reads this, so an unauthenticated request cannot make the Pantry connect
    to the saved server.
    """
    if not is_enabled():
        return []
    raw = _get(db, KEY_MODELS)
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [m for m in value if isinstance(m, str)]


def store_models(db: Session, models: list[str]) -> None:
    _set(db, KEY_MODELS, json.dumps(models))
    db.commit()
