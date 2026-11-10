"""Shared LLM client for BYOK (bring your own key) API calls.

Provides model registry and API call functions used by the Forge,
routine generator, and other AI-powered features.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Model registry ────────────────────────────────────────────────────
# Pricing per million tokens (as of 2026-11)

@dataclass
class ModelConfig:
    """Configuration for an LLM model available in the Forge."""

    id: str                   # Internal ID used by the frontend
    display_name: str         # Shown in the dropdown
    provider: str             # "anthropic", "openai" or "local" (an OpenAI-compatible server set in the Forge settings)
    api_model: str            # Model ID sent to the API
    input_price: float        # $ per 1M input tokens
    output_price: float       # $ per 1M output tokens
    max_tokens: int = 32000   # Max output tokens (current models think first, and thinking counts against this)
    api_type: str = "chat"    # "chat" for chat/completions, "responses" for responses API
    # Output cap when the browser streams the call (GET /v1/forge/prompt): streaming has no HTTP
    # timeout to stay under. None: max_tokens.
    stream_max_tokens: int | None = None


LLM_MODELS: dict[str, ModelConfig] = {
    "haiku": ModelConfig(
        id="haiku",
        display_name="Claude Haiku 5.5",
        provider="anthropic",
        api_model="claude-haiku-5-5",
        stream_max_tokens=128000,  # every current Claude model's output cap
        input_price=0.10,
        output_price=0.50,
    ),
    "sonnet": ModelConfig(
        id="sonnet",
        display_name="Claude Sonnet 5.5",
        provider="anthropic",
        api_model="claude-sonnet-5-5",
        stream_max_tokens=128000,  # every current Claude model's output cap
        input_price=2.00,
        output_price=10.00,
    ),
    "opus": ModelConfig(
        id="opus",
        display_name="Claude Opus 5.5",
        provider="anthropic",
        api_model="claude-opus-5-5",
        stream_max_tokens=128000,  # every current Claude model's output cap
        input_price=4.00,
        output_price=20.00,
    ),
    "fable": ModelConfig(
        id="fable",
        display_name="Claude Fable 5.1",
        provider="anthropic",
        api_model="claude-fable-5-1",
        stream_max_tokens=128000,  # every current Claude model's output cap
        input_price=10.00,
        output_price=50.00,
    ),
    "gpt-6.1-sol": ModelConfig(
        id="gpt-6.1-sol",
        display_name="GPT-6.1 Sol",
        provider="openai",
        api_model="gpt-6.1-sol",
        input_price=2.00,
        output_price=10.00,
        api_type="responses",
    ),
    "gpt-6-astra": ModelConfig(
        id="gpt-6-astra",
        display_name="GPT-6 Astra",
        provider="openai",
        api_model="gpt-6-astra",
        input_price=10.00,
        output_price=50.00,
        api_type="responses",
    ),
    "codex": ModelConfig(
        id="codex",
        display_name="Codex",
        provider="openai",
        api_model="gpt-5.3-codex",
        input_price=1.75,
        output_price=14.00,
        api_type="responses",
    ),
}


LOCAL_PREFIX = "local:"


def local_model_config(model_name: str, max_tokens: int | None = None) -> ModelConfig:
    """ModelConfig for one model served by the configured local server."""
    return ModelConfig(
        id=f"{LOCAL_PREFIX}{model_name}",
        display_name=f"{model_name} (local)",
        provider="local",
        api_model=model_name,
        input_price=0.0,
        output_price=0.0,
        max_tokens=max_tokens or int(os.environ.get("FORGE_LOCAL_MAX_TOKENS", "8192")),
    )


def resolve_local_model(model_id: str, default_model: str = "", detected: list[str] | None = None) -> ModelConfig | None:
    """Map ``local:<model>`` (or the bare ``local``, kept for older clients) to a ModelConfig, else None."""
    if model_id.startswith(LOCAL_PREFIX) and len(model_id) > len(LOCAL_PREFIX):
        return local_model_config(model_id[len(LOCAL_PREFIX):])
    if model_id == "local":
        name = default_model or (detected[0] if detected else "")
        return local_model_config(name) if name else None
    return None


def get_available_models(
    prompt_overhead_tokens: int = 7000,
    output_tokens: int = 6000,
    local_models: list[str] | None = None,
    local_default: str = "",
) -> list[dict[str, Any]]:
    """Return model list with estimated per-generation costs.

    Models detected on the local server come first (default model at the top) and cost nothing.

    Args:
        prompt_overhead_tokens: Approximate input tokens for cost estimation.
        output_tokens: Approximate output tokens for cost estimation.
        local_models: Model ids reported by the local server's /v1/models.
        local_default: Which of them to mark as the default.
    """
    models: list[dict[str, Any]] = []
    ordered = sorted(local_models or [], key=lambda m: (m != local_default, m.lower()))
    for name in ordered:
        cfg = local_model_config(name)
        models.append({
            "id": cfg.id,
            "display_name": cfg.display_name,
            "provider": "local",
            "estimated_cost": "free",
            "estimated_cost_usd": 0.0,
            "default": name == local_default,
        })
    for model in LLM_MODELS.values():
        input_cost = (prompt_overhead_tokens / 1_000_000) * model.input_price
        output_cost = (output_tokens / 1_000_000) * model.output_price
        est_cost = input_cost + output_cost
        models.append({
            "id": model.id,
            "display_name": model.display_name,
            "provider": model.provider,
            "estimated_cost": f"~${est_cost:.3f}" if est_cost < 0.10 else f"~${est_cost:.2f}",
            "estimated_cost_usd": round(est_cost, 4),
        })
    return models


async def call_claude(
    system_prompt: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str = "claude-sonnet-5-5",
    max_tokens: int = 32000,
) -> str:
    """Call Anthropic API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=600.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error: {resp.status_code} — {resp.text}")

    return claude_text(resp.json())


def claude_text(data: dict[str, Any]) -> str:
    """The answer text of a Messages API response; current models put thinking blocks before it."""
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined this request")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text:
        raise RuntimeError(f"Claude returned no text (stop_reason: {data.get('stop_reason')})")
    return text


async def call_openai(
    system_prompt: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str = "gpt-4o",
    max_tokens: int = 8192,
) -> str:
    """Call OpenAI Chat Completions API."""
    openai_messages = [{"role": "system", "content": system_prompt}] + messages

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": openai_messages,
            },
            timeout=600.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error: {resp.status_code} — {resp.text}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def call_openai_responses(
    system_prompt: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str = "gpt-5.3-codex",
    max_tokens: int = 8192,
) -> str:
    """Call OpenAI Responses API (for Codex and similar non-chat models)."""
    input_parts: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]
    input_parts.extend(messages)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": input_parts,
                "max_output_tokens": max_tokens,
            },
            timeout=600.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error: {resp.status_code} — {resp.text}")

    data = resp.json()
    # Responses API returns output as a list of content blocks
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content["text"]
    raise RuntimeError("No text content in OpenAI Responses API output")


async def call_local(
    system_prompt: str,
    messages: list[dict[str, str]],
    api_key: str,
    model: str,
    max_tokens: int,
    base_url: str,
) -> str:
    """Call a local OpenAI-compatible chat server.

    Local models are slower than hosted ones and some return their reasoning inline, so the timeout is long
    (``FORGE_LOCAL_TIMEOUT``, default 900 s) and any <think> block is removed before the reply reaches the parser.
    """
    local_messages = [{"role": "system", "content": system_prompt}] + messages
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={"model": model, "max_tokens": max_tokens, "messages": local_messages},
            timeout=float(os.environ.get("FORGE_LOCAL_TIMEOUT", "900")),
        )

    if resp.status_code != 200:
        # The body is not returned: it can carry whatever the server wants to say, including echoed credentials.
        raise RuntimeError(f"Local LLM server answered HTTP {resp.status_code}")

    try:
        text = resp.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("Local LLM server returned a reply that is not an OpenAI chat completion")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
