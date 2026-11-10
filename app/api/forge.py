"""Forge endpoints — spec serving, AI package generation, and GitHub repo creation.

The Forge (AI-powered package builder) uses these endpoints to:
1. Get the SDK authoring spec (auto-generated from source code)
2. Generate complete packages from natural language descriptions
3. Create GitHub repos from generated files and push them
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from .admin_key import is_admin_key, require_admin_key
from ..services import local_llm

from ..rate_limiter import RateLimiter, _forge_generate_cap_per_hour, client_ip

router = APIRouter()
logger = logging.getLogger(__name__)

# Rate limiter for forge generation: per-IP cap from
# FORGE_GENERATE_RATE_LIMIT_PER_HOUR. Uses cap_source so the cap is re-read on
# each request — runtime tuning of the setting (after a
# get_settings.cache_clear() or fresh process start with a different env var)
# takes effect without re-importing this module.
_generate_limiter = RateLimiter(cap_source=_forge_generate_cap_per_hour)


class ForgeGenerateRequest(BaseModel):
    """Request body for package generation."""

    description: str
    model: str = "sonnet"  # Model ID from FORGE_MODELS registry
    llm_api_key: str
    conversation_history: list[dict[str, str]] | None = None
    # The package as it is now: a revision's reply carries only the files it changes.
    current_files: list[dict[str, str]] | None = None
    package_name: str = ""
    display_name: str = ""


class ForgeParseRequest(BaseModel):
    """A reply the browser got from a hosted model with the user's own key, and the package it revises."""

    text: str
    current_files: list[dict[str, str]] | None = None
    package_name: str = ""
    display_name: str = ""


# Parsing is cheap but runs static analysis on a temp copy; the browser calls it once per generation.
_parse_limiter = RateLimiter(requests_per_hour=240)
_MAX_REPLY_CHARS = 2_000_000


def _current(files: list[dict[str, str]] | None):
    from ..services.forge_generator import GeneratedFile, language_for

    return [
        GeneratedFile(filename=f["filename"], content=f.get("content", ""), language=language_for(f["filename"], f.get("language")))
        for f in files or []
        if f.get("filename")
    ]


def _reply_json(reply) -> dict[str, Any]:
    """The /generate and /parse answer: the whole package after the reply, and what it touched."""
    result = reply.result
    response: dict[str, Any] = {
        "package_name": result.package_name,
        "display_name": result.display_name,
        "explanation": result.explanation,
        "files": [{"filename": f.filename, "content": f.content, "language": f.language} for f in result.files],
        "changed": reply.changed,
        "deleted": reply.deleted,
        "complete": reply.complete,
    }
    if result.validation:
        response["validation"] = {
            "passed": result.validation.passed,
            "errors": result.validation.errors,
            "warnings": result.validation.warnings,
            "dangerous_patterns": result.validation.dangerous_patterns,
        }
    return response


@router.get("/v1/forge/models")
def get_forge_models(db: Session = Depends(get_db)):
    """Return available models with estimated per-generation costs.

    Models found on the configured local server come first, with the default marked ``"default": true``.
    """
    from ..services.llm_client import get_available_models
    cfg = local_llm.get_config(db)
    return {"models": get_available_models(
        local_models=local_llm.get_stored_models(db),
        local_default=cfg.default_model,
    )}


# ── Local LLM server settings ─────────────────────────────────────────

def _require_enabled() -> None:
    """Local LLM endpoints do not exist unless FORGE_LOCAL_LLM_ENABLED is set."""
    if not local_llm.is_enabled():
        raise HTTPException(404, "Not Found")


@router.get("/v1/forge/features")
def get_forge_features():
    """Which optional Forge features this Pantry has switched on. The web interface hides what is off."""
    from ..config import get_settings
    settings = get_settings()
    return {
        "local_llm": local_llm.is_enabled(),
        # Adding a package from a repository only works when an admin key is configured.
        "private_packages": bool(getattr(settings, "admin_api_key", "")),
        # Saving or probing the local LLM server needs the same key.
        "admin_key_configured": bool(getattr(settings, "admin_api_key", "")),
        "upstream_mirror": bool(getattr(settings, "pantry_upstream_url", "")),
    }


def _require_admin(x_admin_key: str | None) -> None:
    """Changing the saved server and probing a server both need the admin key, always."""
    require_admin_key(x_admin_key, "Changing or probing the local LLM server")


class LocalLLMSettingsRequest(BaseModel):
    base_url: str
    api_key: str | None = None   # None keeps the stored key, "" clears it
    default_model: str | None = None


class LocalLLMDetectRequest(BaseModel):
    base_url: str | None = None  # None tests the saved server
    api_key: str | None = None


def _settings_view(cfg: local_llm.LocalLLMConfig, show_url: bool = True) -> dict[str, Any]:
    return {
        "base_url": cfg.base_url if show_url else "",
        "has_api_key": bool(cfg.api_key),
        "default_model": cfg.default_model,
        "source": cfg.source,
    }


@router.get("/v1/forge/local-llm")
def get_local_llm_settings(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Current local server settings. The API key is never returned, only whether one is set.

    The server address is internal, so it is only included for a caller that sends the admin key.
    """
    _require_enabled()
    return _settings_view(local_llm.get_config(db), show_url=is_admin_key(x_admin_key))


@router.put("/v1/forge/local-llm")
def put_local_llm_settings(
    body: LocalLLMSettingsRequest,
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Save the local OpenAI-compatible server (llama-swap, llama.cpp, vLLM, Ollama)."""
    _require_enabled()
    _require_admin(x_admin_key)
    try:
        cfg = local_llm.save_config(db, body.base_url, body.api_key, body.default_model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Refresh the model list the public endpoint serves, so a new server shows its models without another step.
    # An unreachable server just leaves an empty list.
    try:
        local_llm.store_models(db, local_llm.detect_models(cfg.base_url, cfg.api_key))
    except (local_llm.LocalLLMError, ValueError):
        local_llm.store_models(db, [])
    return _settings_view(cfg)


@router.post("/v1/forge/local-llm/detect")
def detect_local_llm_models(
    body: LocalLLMDetectRequest | None = None,
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    """Ask a server which models it serves.

    Tests the supplied address and key, or the saved ones when omitted. The saved API key is only ever sent to
    the saved address: a different address is probed with the key supplied in the request (or none).
    """
    _require_enabled()
    _require_admin(x_admin_key)
    body = body or LocalLLMDetectRequest()
    saved = local_llm.get_config(db)
    try:
        supplied = local_llm.normalise_base_url(body.base_url) if body.base_url is not None else None
    except ValueError as e:
        return {"ok": False, "error": str(e), "models": []}
    is_saved_server = supplied is None or supplied == saved.base_url
    base_url = saved.base_url if is_saved_server else supplied
    if is_saved_server:
        api_key = body.api_key if body.api_key is not None else saved.api_key
    else:
        api_key = body.api_key or ""
    try:
        models = local_llm.detect_models(base_url, api_key)
    except (local_llm.LocalLLMError, ValueError) as e:
        return {"ok": False, "error": str(e), "models": []}
    if is_saved_server:
        local_llm.store_models(db, models)
    return {"ok": True, "models": models, "default_model": saved.default_model}


@router.get("/v1/forge/spec")
def get_forge_spec(format: Literal["json", "markdown"] = Query("json")):
    """Get the Forge authoring spec.

    Returns the full SDK interface spec, manifest schema, file layout
    conventions, and validation rules — auto-generated from the SDK.

    Query params:
        format: "json" (default) for structured data, "markdown" for human-readable text.
    """
    from jarvis_command_sdk.forge import generate_spec, generate_spec_markdown

    if format == "markdown":
        return {"spec": generate_spec_markdown()}

    return generate_spec()


@router.post("/v1/forge/generate")
async def generate_package(body: ForgeGenerateRequest, request: Request, db: Session = Depends(get_db)):
    """Generate a Jarvis package from a natural language description.

    BYOK — the user provides their own LLM API key. Used once, never stored.
    Rate-limited by client IP.
    """
    from ..config import get_settings

    ip = client_ip(request)
    if not _generate_limiter.check(f"forge-generate:{ip}"):
        raise HTTPException(429, "Rate limit exceeded. Try again later.")

    if not body.description.strip():
        raise HTTPException(400, "Description is required")
    is_local = body.model == "local" or body.model.startswith("local:")
    if not is_local and not get_settings().bypass_llm_key and not body.llm_api_key.strip():
        raise HTTPException(400, "API key is required")

    from ..services.forge_generator import generate_package as do_generate

    local_cfg = local_llm.get_config(db)
    try:
        reply = await do_generate(
            description=body.description.strip(),
            llm_api_key=body.llm_api_key.strip(),
            model_id=body.model,
            conversation_history=body.conversation_history,
            local_config=local_cfg,
            local_models=local_llm.get_stored_models(db),
            current_files=_current(body.current_files),
            package_name=body.package_name,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _reply_json(reply)


@router.get("/v1/forge/prompt")
def get_forge_prompt(model: str = Query("sonnet"), tools: bool = Query(False)):
    """What the browser needs to call a hosted model itself with the user's key: the system prompt
    and the model's API name and output cap. The key never reaches Pantry, and no request here waits
    on a model (a long generation outlived proxy timeouts). Local models keep POST /v1/forge/generate:
    their server and key are Pantry's settings. ``tools=true``: the browser gives the model the package
    reading tools (GET /v1/forge/packages…), so the prompt tells it about them."""
    from ..services.forge_generator import _build_system_prompt
    from ..services.llm_client import LLM_MODELS

    cfg = LLM_MODELS.get(model)
    if cfg is None:
        raise HTTPException(400, f"Unknown model: {model}")
    return {
        "model": cfg.id,
        "provider": cfg.provider,
        "api_type": cfg.api_type,
        "api_model": cfg.api_model,
        "max_tokens": cfg.stream_max_tokens or cfg.max_tokens,
        "system": _build_system_prompt(tools=tools),
    }


@router.post("/v1/forge/parse")
def parse_forge_reply(body: ForgeParseRequest, request: Request):
    """Apply a model's reply to the package: its files replace or join the current ones, then the
    whole package is validated. "complete" is false when the reply was cut off before === END ===."""
    from ..services.forge_generator import merge_forge_reply

    if not _parse_limiter.check(f"forge-parse:{client_ip(request)}"):
        raise HTTPException(429, "Rate limit exceeded. Try again later.")
    if len(body.text) > _MAX_REPLY_CHARS:
        raise HTTPException(413, "Reply too large")
    reply = merge_forge_reply(body.text, _current(body.current_files), body.package_name, body.display_name)
    # The model ran in the browser: this is Pantry's one view of each generation (never the code).
    v = reply.result.validation
    logger.info(
        "Forge reply applied: package=%s chars=%d changed=%s deleted=%s complete=%s files=%d validation=%s",
        reply.result.package_name, len(body.text), reply.changed, reply.deleted, reply.complete, len(reply.result.files),
        "none" if v is None else ("passed" if v.passed else f"{len(v.errors)} errors: {'; '.join(v.errors)[:300]}"),
    )
    return _reply_json(reply)


# ── Package reading tools: the browser's model looks at published packages as references ──

# A generation may make a few dozen tool calls; GitHub requests behind them are cached.
_package_limiter = RateLimiter(requests_per_hour=600)
_ComponentType = Literal["command", "agent", "device_protocol", "device_manager", "bundle"]


def _package_files_call(request: Request, fn):
    from ..services import package_files

    if not _package_limiter.check(f"forge-packages:{client_ip(request)}"):
        raise HTTPException(429, "Rate limit exceeded. Try again later.")
    try:
        return fn(package_files)
    except package_files.PackageNotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except package_files.PackageFilesError as e:
        raise HTTPException(502, str(e))


@router.get("/v1/forge/packages")
def search_forge_packages(
    request: Request,
    q: str = Query("", max_length=200),
    type: _ComponentType | None = Query(None),
    db: Session = Depends(get_db),
):
    """Published packages matching a query, optionally holding a component of one type."""
    return _package_files_call(request, lambda pf: {"packages": pf.search(db, q, type)})


@router.get("/v1/forge/packages/{name}/files")
def list_forge_package_files(name: str, request: Request, db: Session = Depends(get_db)):
    """A published package's files (path and size) at its latest published version, from GitHub."""
    return _package_files_call(request, lambda pf: pf.list_files(db, name))


@router.get("/v1/forge/packages/{name}/file")
def read_forge_package_file(name: str, request: Request, path: str = Query(..., max_length=500), db: Session = Depends(get_db)):
    """One text file of a published package at its latest published version (up to 200 KB)."""
    return _package_files_call(request, lambda pf: pf.read_file(db, name, path))


class CreateRepoRequest(BaseModel):
    """Request body for creating a GitHub repo from Forge output."""

    package_name: str
    files: list[dict[str, str]]  # [{filename, content}]
    github_token: str


@router.post("/v1/forge/create-repo")
async def create_repo(body: CreateRepoRequest):
    """Create a GitHub repo and push generated files.

    Uses the user's GitHub token (from OAuth with public_repo scope).
    Creates a public repo named jarvis-command-{package_name}.
    """
    if not body.github_token.strip():
        raise HTTPException(400, "GitHub token is required")
    if not body.files:
        raise HTTPException(400, "No files to push")

    token = body.github_token.strip()
    repo_name = f"jarvis-command-{body.package_name.replace('_', '-')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Get authenticated user
        user_resp = await client.get(
            "https://api.github.com/user",
            headers=headers,
        )
        if user_resp.status_code != 200:
            raise HTTPException(401, "Invalid GitHub token")
        username = user_resp.json()["login"]

        # 2. Create repo
        create_resp = await client.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json={
                "name": repo_name,
                "description": f"Jarvis package — created with Forge",
                "private": False,
                "auto_init": False,
            },
        )

        if create_resp.status_code == 422:
            # Repo already exists — check if it belongs to this user
            detail = create_resp.json().get("errors", [{}])
            if any("name already exists" in str(e) for e in detail):
                raise HTTPException(
                    409,
                    f"Repository '{repo_name}' already exists. Delete it or rename your package.",
                )
            raise HTTPException(422, f"GitHub error: {create_resp.text}")
        elif create_resp.status_code not in (200, 201):
            raise HTTPException(502, f"Failed to create repo: {create_resp.text}")

        repo_url = f"https://github.com/{username}/{repo_name}"

        # 3. Push files via Contents API (one at a time for simplicity)
        for file_data in body.files:
            filename = file_data["filename"]
            content_b64 = base64.b64encode(
                file_data["content"].encode("utf-8")
            ).decode("ascii")

            put_resp = await client.put(
                f"https://api.github.com/repos/{username}/{repo_name}/contents/{filename}",
                headers=headers,
                json={
                    "message": f"Add {filename} (via Forge)",
                    "content": content_b64,
                },
            )

            if put_resp.status_code not in (200, 201):
                logger.error(
                    "Failed to push %s: %s %s",
                    filename,
                    put_resp.status_code,
                    put_resp.text[:200],
                )
                raise HTTPException(
                    502, f"Failed to push {filename}: {put_resp.status_code}"
                )

    return {"repo_url": repo_url, "repo_name": repo_name}
