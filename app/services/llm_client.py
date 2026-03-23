"""Shared LLM client for BYOK (bring your own key) API calls.

Provides model registry and API call functions used by the Forge,
routine generator, and other AI-powered features.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Model registry ────────────────────────────────────────────────────
# Pricing per million tokens (as of 2026-03)

@dataclass
class ModelConfig:
    """Configuration for an LLM model available in the Forge."""

    id: str                   # Internal ID used by the frontend
    display_name: str         # Shown in the dropdown
    provider: str             # "anthropic" or "openai"
    api_model: str            # Model ID sent to the API
    input_price: float        # $ per 1M input tokens
    output_price: float       # $ per 1M output tokens
    max_tokens: int = 8192    # Max output tokens
    api_type: str = "chat"    # "chat" for chat/completions, "responses" for responses API


LLM_MODELS: dict[str, ModelConfig] = {
    "haiku": ModelConfig(
        id="haiku",
        display_name="Claude Haiku 4.5",
        provider="anthropic",
        api_model="claude-haiku-4-5-20251001",
        input_price=1.00,
        output_price=5.00,
    ),
    "sonnet": ModelConfig(
        id="sonnet",
        display_name="Claude Sonnet 4",
        provider="anthropic",
        api_model="claude-sonnet-4-20250514",
        input_price=3.00,
        output_price=15.00,
    ),
    "opus": ModelConfig(
        id="opus",
        display_name="Claude Opus 4",
        provider="anthropic",
        api_model="claude-opus-4-20250514",
        input_price=15.00,
        output_price=75.00,
    ),
    "gpt-4o": ModelConfig(
        id="gpt-4o",
        display_name="GPT-4o",
        provider="openai",
        api_model="gpt-4o",
        input_price=2.50,
        output_price=10.00,
    ),
    "chatgpt-5": ModelConfig(
        id="chatgpt-5",
        display_name="ChatGPT-5",
        provider="openai",
        api_model="chatgpt-5",
        input_price=5.00,
        output_price=15.00,
    ),
    "codex": ModelConfig(
        id="codex",
        display_name="Codex",
        provider="openai",
        api_model="gpt-5.3-codex",
        input_price=1.50,
        output_price=6.00,
        api_type="responses",
    ),
}


def get_available_models(
    prompt_overhead_tokens: int = 7000,
    output_tokens: int = 6000,
) -> list[dict[str, Any]]:
    """Return model list with estimated per-generation costs.

    Args:
        prompt_overhead_tokens: Approximate input tokens for cost estimation.
        output_tokens: Approximate output tokens for cost estimation.
    """
    models: list[dict[str, Any]] = []
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
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 8192,
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
            timeout=120.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error: {resp.status_code} — {resp.text}")

    data = resp.json()
    return data["content"][0]["text"]


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
            timeout=120.0,
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
            timeout=120.0,
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
