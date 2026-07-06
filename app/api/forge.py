"""Forge endpoints — spec serving, AI package generation, and GitHub repo creation.

The Forge (AI-powered package builder) uses these endpoints to:
1. Get the SDK authoring spec (auto-generated from source code)
2. Generate complete packages from natural language descriptions
3. Create GitHub repos from generated files and push them
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..rate_limiter import RateLimiter, _forge_generate_cap_per_hour

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


@router.get("/v1/forge/models")
def get_forge_models():
    """Return available models with estimated per-generation costs."""
    from ..services.llm_client import get_available_models
    return {"models": get_available_models()}


@router.get("/v1/forge/spec")
def get_forge_spec(format: str = Query("json", enum=["json", "markdown"])):
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
async def generate_package(body: ForgeGenerateRequest, request: Request):
    """Generate a Jarvis package from a natural language description.

    BYOK — the user provides their own LLM API key. Used once, never stored.
    Rate-limited by client IP.
    """
    from ..config import get_settings

    client_ip = request.client.host if request.client else "unknown"
    if not _generate_limiter.check(f"forge-generate:{client_ip}"):
        raise HTTPException(429, "Rate limit exceeded. Try again later.")

    if not body.description.strip():
        raise HTTPException(400, "Description is required")
    if not get_settings().bypass_llm_key and not body.llm_api_key.strip():
        raise HTTPException(400, "API key is required")

    from ..services.forge_generator import generate_package as do_generate

    try:
        result = await do_generate(
            description=body.description.strip(),
            llm_api_key=body.llm_api_key.strip(),
            model_id=body.model,
            conversation_history=body.conversation_history,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    response: dict[str, Any] = {
        "package_name": result.package_name,
        "display_name": result.display_name,
        "explanation": result.explanation,
        "files": [
            {
                "filename": f.filename,
                "content": f.content,
                "language": f.language,
            }
            for f in result.files
        ],
    }

    if result.validation:
        response["validation"] = {
            "passed": result.validation.passed,
            "errors": result.validation.errors,
            "warnings": result.validation.warnings,
            "dangerous_patterns": result.validation.dangerous_patterns,
        }

    return response


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
