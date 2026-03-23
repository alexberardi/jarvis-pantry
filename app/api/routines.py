"""Routine generation endpoints — AI-powered routine builder.

Generates multi-step voice routines from a node's installed commands
using BYOK (bring your own key) LLM calls.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class RoutineGenerateRequest(BaseModel):
    """Request body for routine generation."""

    available_commands: list[dict[str, Any]]
    model: str = "haiku"
    llm_api_key: str
    user_prompt: str | None = None


@router.get("/v1/routines/models")
def get_routine_models() -> dict[str, Any]:
    """Return available models with estimated per-generation costs for routines."""
    from ..services.llm_client import get_available_models
    from ..services.routine_generator import _PROMPT_OVERHEAD_TOKENS, _OUTPUT_TOKENS

    return {"models": get_available_models(
        prompt_overhead_tokens=_PROMPT_OVERHEAD_TOKENS,
        output_tokens=_OUTPUT_TOKENS,
    )}


@router.post("/v1/routines/generate")
async def generate_routines(body: RoutineGenerateRequest) -> dict[str, Any]:
    """Generate routines from a node's installed commands.

    BYOK — the user provides their own LLM API key. Used once, never stored.
    """
    if not body.llm_api_key.strip():
        raise HTTPException(400, "API key is required")
    if not body.available_commands:
        raise HTTPException(400, "At least one command is required")

    from ..services.routine_generator import generate_routines as do_generate

    try:
        result = await do_generate(
            available_commands=body.available_commands,
            llm_api_key=body.llm_api_key.strip(),
            model_id=body.model,
            user_prompt=body.user_prompt,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    return {
        "routines": result.routines,
        "explanation": result.explanation,
        "validation_warnings": result.warnings,
    }
