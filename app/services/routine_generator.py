"""AI-powered routine generator.

Takes a node's installed commands and generates Routine JSON objects
that combine commands into useful multi-step voice routines.

Uses BYOK (bring your own key) — same pattern as the Forge.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .llm_client import (
    LLM_MODELS,
    call_claude,
    call_openai,
    call_openai_responses,
)

logger = logging.getLogger(__name__)


# Approximate prompt size for cost estimation (routines are smaller than forge)
_PROMPT_OVERHEAD_TOKENS = 4000
_OUTPUT_TOKENS = 3000

# ── Reference routines ────────────────────────────────────────────────

REFERENCE_ROUTINES: list[dict[str, Any]] = [
    {
        "id": "good_morning",
        "name": "Good Morning",
        "trigger_phrases": ["good morning", "morning routine", "start my day"],
        "steps": [
            {"command": "control_device", "args": [{"key": "floor", "value": "Downstairs"}, {"key": "action", "value": "turn_on"}], "label": "lights"},
            {"command": "get_weather", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "weather"},
            {"command": "get_calendar_events", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "calendar"},
        ],
        "response_instruction": "Give a cheerful morning briefing with weather and calendar highlights.",
        "response_length": "short",
        "background": None,
    },
    {
        "id": "morning_briefing",
        "name": "Morning Briefing",
        "trigger_phrases": ["morning briefing", "daily briefing", "give me my briefing", "what's happening today", "catch me up"],
        "steps": [
            {"command": "get_weather", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "weather"},
            {"command": "get_calendar_events", "args": [{"key": "resolved_datetimes", "value": "today"}], "label": "calendar"},
            {"command": "get_news", "args": [{"key": "category", "value": "general"}, {"key": "count", "value": "3"}], "label": "news"},
        ],
        "response_instruction": "Deliver a morning briefing in a natural, flowing narrative style.",
        "response_length": "medium",
        "background": None,
    },
    {
        "id": "good_night",
        "name": "Good Night",
        "trigger_phrases": ["good night", "bedtime", "going to bed", "time for bed"],
        "steps": [
            {"command": "control_device", "args": [{"key": "floor", "value": "Downstairs"}, {"key": "action", "value": "turn_off"}], "label": "lights"},
            {"command": "get_calendar_events", "args": [{"key": "resolved_datetimes", "value": "tomorrow"}], "label": "tomorrow"},
        ],
        "response_instruction": "Brief goodnight with tomorrow's first appointment if any.",
        "response_length": "short",
        "background": None,
    },
]


@dataclass
class RoutineGenerationResult:
    """Result of a routine generation request."""

    routines: list[dict[str, Any]]
    explanation: str
    warnings: list[str] = field(default_factory=list)


def _slugify(name: str) -> str:
    """Convert a display name to a slug ID (lowercase, underscores)."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug


def _build_system_prompt(available_commands: list[dict[str, Any]]) -> str:
    """Build the routine generation system prompt.

    Includes the Routine data model spec, all installed commands with
    metadata, and reference routines showing proper structure.
    """
    # Format command metadata
    commands_section = _format_commands(available_commands)
    reference_section = json.dumps(REFERENCE_ROUTINES, indent=2)

    return f"""\
You are a Jarvis routine generator. You create multi-step voice routines \
that combine installed commands into useful automations.

## Routine Data Model

Each routine is a JSON object with these fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| id | string | yes | Slugified name (lowercase, underscores for spaces/special chars) |
| name | string | yes | Human-readable display name |
| trigger_phrases | string[] | yes | 3+ natural voice phrases that activate this routine |
| steps | object[] | yes | Ordered list of commands to execute |
| response_instruction | string | yes | How to format the combined response for TTS |
| response_length | string | yes | "short", "medium", or "long" |
| background | null | yes | Set to null for on-demand routines |

### Step Object

Each step in the `steps` array:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| command | string | yes | Command name (must match an installed command) |
| args | object[] | yes | Array of {{key, value}} objects (NOT a dict) |
| label | string | yes | Short descriptive word for this step's output |

### Args Format

Args is an array of key-value objects, NOT a dictionary:
```json
"args": [
  {{"key": "resolved_datetimes", "value": "today"}},
  {{"key": "category", "value": "general"}}
]
```

## Installed Commands

{commands_section}

## Reference Routines

These show the correct structure. Use them as examples:

```json
{reference_section}
```

## Rules

1. `id` MUST be the slugified version of `name` (lowercase, underscores for spaces/special chars)
2. `trigger_phrases` MUST have at least 3 natural voice phrases
3. Every `steps[].command` MUST be one of the installed commands listed above
4. `args` MUST be an array of {{"key": "...", "value": "..."}} objects
5. `response_length` MUST be "short", "medium", or "long"
6. `background` MUST be null for on-demand routines (which is most common)
7. Each step MUST have a `label` (a short descriptive word)
8. Only use commands from the installed commands list — never invent commands
9. Arg keys should correspond to parameter names from the command metadata when possible
10. Create routines that are genuinely useful combinations, not just single-command wrappers

## Output Format

Respond with ONLY a JSON object (no markdown fences, no explanation outside the JSON):

{{
  "routines": [
    {{
      "id": "...",
      "name": "...",
      "trigger_phrases": ["...", "...", "..."],
      "steps": [...],
      "response_instruction": "...",
      "response_length": "short",
      "background": null
    }}
  ],
  "explanation": "Brief description of what was generated"
}}
"""


def _format_commands(commands: list[dict[str, Any]]) -> str:
    """Format available commands into a readable section for the prompt."""
    if not commands:
        return "(No commands available)"

    parts: list[str] = []
    for cmd in commands:
        name: str = cmd.get("name", cmd.get("command_name", "unknown"))
        desc: str = cmd.get("description", "No description")
        keywords: list[str] = cmd.get("keywords", [])
        examples: list[str] = cmd.get("examples", [])
        params: list[dict[str, Any]] = cmd.get("parameters", [])

        lines: list[str] = [f"### {name}", f"Description: {desc}"]

        if keywords:
            lines.append(f"Keywords: {', '.join(keywords)}")

        if params:
            param_lines: list[str] = []
            for p in params:
                pname: str = p.get("name", "unknown")
                ptype: str = p.get("type", p.get("param_type", "string"))
                required: bool = p.get("required", False)
                pdesc: str = p.get("description", "")
                enum_vals: list[str] | None = p.get("enum_values")
                parts_p: list[str] = [f"  - `{pname}` ({ptype})"]
                if required:
                    parts_p.append("[required]")
                if pdesc:
                    parts_p.append(f"— {pdesc}")
                if enum_vals:
                    parts_p.append(f"(options: {', '.join(str(v) for v in enum_vals)})")
                param_lines.append(" ".join(parts_p))
            lines.append("Parameters:")
            lines.extend(param_lines)

        if examples:
            lines.append(f"Voice examples: {', '.join(repr(e) for e in examples[:3])}")

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _validate_routines(
    routines: list[dict[str, Any]],
    command_names: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate generated routines against installed commands.

    Returns:
        Tuple of (valid_routines, warnings).
    """
    valid: list[dict[str, Any]] = []
    warnings: list[str] = []

    for routine in routines:
        name: str = routine.get("name", "")
        routine_id: str = routine.get("id", "")

        # Check trigger_phrases is a non-empty list
        trigger_phrases = routine.get("trigger_phrases")
        if not isinstance(trigger_phrases, list) or len(trigger_phrases) == 0:
            warnings.append(f"Routine '{name}': missing or empty trigger_phrases, skipped")
            continue

        # Check steps is a non-empty list
        steps = routine.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            warnings.append(f"Routine '{name}': missing or empty steps, skipped")
            continue

        # Check response_length is valid
        response_length: str = routine.get("response_length", "")
        if response_length not in ("short", "medium", "long"):
            warnings.append(f"Routine '{name}': invalid response_length '{response_length}', defaulting to 'medium'")
            routine["response_length"] = "medium"

        # Check id matches slugified name
        expected_id = _slugify(name)
        if routine_id != expected_id:
            warnings.append(f"Routine '{name}': id '{routine_id}' does not match slugified name '{expected_id}', auto-fixed")
            routine["id"] = expected_id

        # Check all step commands exist
        has_unknown_command = False
        for step in steps:
            cmd_name: str = step.get("command", "")
            if cmd_name not in command_names:
                warnings.append(f"Routine '{name}': step uses unknown command '{cmd_name}', routine removed")
                has_unknown_command = True
                break

        if has_unknown_command:
            continue

        # Warn on potentially bad arg keys (non-blocking)
        for step in steps:
            args = step.get("args", [])
            if not isinstance(args, list):
                warnings.append(f"Routine '{name}': step '{step.get('command', '')}' has non-list args, should be [{{key, value}}]")

        valid.append(routine)

    return valid, warnings


async def generate_routines(
    available_commands: list[dict[str, Any]],
    llm_api_key: str,
    model_id: str = "haiku",
    user_prompt: str | None = None,
) -> RoutineGenerationResult:
    """Generate routines from a node's installed commands.

    Args:
        available_commands: List of command metadata dicts (name, description,
            parameters, keywords, examples).
        llm_api_key: User's API key (used once, not stored).
        model_id: Model ID from LLM_MODELS registry.
        user_prompt: Optional user request to guide generation.

    Returns:
        RoutineGenerationResult with generated routines and any warnings.
    """
    model = LLM_MODELS.get(model_id)
    if not model:
        raise ValueError(f"Unknown model: {model_id}. Available: {', '.join(LLM_MODELS.keys())}")

    system_prompt = _build_system_prompt(available_commands)

    if user_prompt and user_prompt.strip():
        user_message = f"Generate 3-5 routines prioritizing this request: {user_prompt.strip()}"
    else:
        user_message = "Generate 3-5 diverse routines that showcase useful combinations of the installed commands"

    messages: list[dict[str, str]] = [{"role": "user", "content": user_message}]

    logger.info(
        "Routine generator, model=%s (%s), commands=%d, prompt_len=%d",
        model_id, model.api_model, len(available_commands), len(system_prompt),
    )

    if model.provider == "anthropic":
        raw = await call_claude(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai" and model.api_type == "responses":
        raw = await call_openai_responses(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai":
        raw = await call_openai(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    else:
        raise ValueError(f"Unsupported provider: {model.provider}")

    logger.info("Routine generator LLM response received, len=%d", len(raw))

    # Parse JSON response
    parsed = _parse_response(raw)

    # Extract command names for validation
    command_names: set[str] = set()
    for cmd in available_commands:
        name = cmd.get("name", cmd.get("command_name", ""))
        if name:
            command_names.add(name)

    raw_routines: list[dict[str, Any]] = parsed.get("routines", [])
    explanation: str = parsed.get("explanation", "")

    valid_routines, warnings = _validate_routines(raw_routines, command_names)

    if not valid_routines and raw_routines:
        warnings.append("All generated routines were invalid after validation")

    return RoutineGenerationResult(
        routines=valid_routines,
        explanation=explanation,
        warnings=warnings,
    )


def _parse_response(text: str) -> dict[str, Any]:
    """Parse the JSON response from the LLM."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse routine generator response: {e}\nRaw: {text[:500]}")
