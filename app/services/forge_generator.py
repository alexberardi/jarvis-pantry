"""AI-powered package generator for the Forge.

Takes a natural language description of what the user wants, fetches the
SDK authoring spec, and calls an LLM to generate the complete package
(command.py, jarvis_command.yaml, README.md).

Uses BYOK (bring your own key) — same pattern as security_review.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from jarvis_command_sdk.forge import generate_spec_markdown

from .llm_client import (
    LLM_MODELS,
    call_claude,
    call_openai,
    call_openai_responses,
)

logger = logging.getLogger(__name__)


# Backward compatibility alias
FORGE_MODELS = LLM_MODELS


@dataclass
class GeneratedFile:
    """A file produced by the Forge."""

    filename: str
    content: str
    language: str  # for syntax highlighting: "python", "yaml", "markdown"


@dataclass
class ForgeValidation:
    """Results of pre-publish validation on generated code."""

    passed: bool
    errors: list[str]
    warnings: list[str]
    dangerous_patterns: list[str]


@dataclass
class ForgeResult:
    """Result of a Forge generation request."""

    files: list[GeneratedFile]
    explanation: str
    package_name: str
    display_name: str
    validation: ForgeValidation | None = None


def _build_system_prompt() -> str:
    """Build the Forge system prompt from the SDK spec."""
    spec_md = generate_spec_markdown()
    return f"""\
You are the Jarvis Forge — an AI that generates complete, working Jarvis \
packages from natural language descriptions. You can generate any component type: \
voice commands (IJarvisCommand), background agents (IJarvisAgent), device protocols \
(IJarvisDeviceProtocol), device managers (IJarvisDeviceManager), or multi-component \
bundles combining several of these.

You have access to the full SDK authoring reference below. Use it to generate \
correct, valid packages that will pass the Pantry validation pipeline (static \
analysis + container tests).

## SDK Authoring Reference

{spec_md}

## Your Task

**Before generating any code**, evaluate whether the user's description is clear enough \
to produce a correct, complete package. If anything is ambiguous or underspecified, \
ASK CLARIFYING QUESTIONS first. Do NOT generate code until you are confident about:

- What component type(s) to build (command, agent, protocol, manager, or bundle)
- What the command/agent should actually do (core logic)
- Whether it needs external APIs (and if so, which ones and whether they require API keys)
- Whether it needs pip packages
- What parameters the user can pass via voice (if any)
- Any platform restrictions

When asking questions, respond with a JSON object like this:
{{
  "package_name": "",
  "display_name": "",
  "explanation": "I have a few questions before I generate your package:\\n\\n1. ...\\n2. ...\\n3. ...",
  "files": []
}}

Use an empty `files` array and empty `package_name`/`display_name` when asking questions. \
Put your questions in the `explanation` field. The user will answer and you can then \
generate the full package.

If the user's description is already clear and unambiguous (e.g., "a command that \
tells dad jokes using the icanhazdadjoke API"), skip the questions and generate directly.

Once you are ready to generate, produce a complete package with these files:

1. **Entry file** — Full implementation of the appropriate interface:
   - `command.py` for commands (IJarvisCommand)
   - `agent.py` for agents (IJarvisAgent)
   - `protocol.py` for device protocols (IJarvisDeviceProtocol)
   - `manager.py` for device managers (IJarvisDeviceManager)
   - For bundles: multiple entry files in convention directories
2. **jarvis_command.yaml** (single component) or **jarvis_package.yaml** (bundle) — Valid manifest
3. **README.md** — Description, usage examples, configuration
4. **LICENSE** — MIT license text (required for Pantry submission)

## Output Format

Respond with ONLY a JSON object (no markdown fences, no explanation outside the JSON):
{{
  "package_name": "snake_case_name",
  "display_name": "Human Readable Name",
  "explanation": "Brief explanation of what was generated and how to use it",
  "files": [
    {{
      "filename": "command.py",
      "content": "...full file content...",
      "language": "python"
    }},
    {{
      "filename": "jarvis_command.yaml",
      "content": "...full YAML content...",
      "language": "yaml"
    }},
    {{
      "filename": "README.md",
      "content": "...full markdown content...",
      "language": "markdown"
    }},
    {{
      "filename": "LICENSE",
      "content": "MIT License\\n\\nCopyright (c) 2026\\n\\nPermission is hereby granted, free of charge, to any person obtaining a copy...",
      "language": "text"
    }}
  ]
}}

## Critical Rules (violations will fail validation)

### Naming
- command_name MUST be snake_case (e.g., "get_weather", "tell_dad_joke")
- The manifest `name` field MUST exactly match the command_name property in command.py
- The JSON response `package_name` MUST also match both of these

### Manifest Format
- author field MUST be: `author: {{ github: "community" }}` — NOT name/email
- min_jarvis_version MUST be "0.9.0"
- schema_version MUST be 1
- version should be "0.1.0"
- categories MUST be from the valid list (see SDK reference above)
- Do NOT invent manifest fields — only use fields from the schema reference

### Constructor Signatures (CRITICAL — wrong kwargs cause TypeError)
- JarvisParameter: `JarvisParameter(name, param_type, required=False, description=None, default=None, enum_values=None)`
  - Use `param_type=` NOT `parameter_type=`
  - Use `default=` NOT `default_value=`
- JarvisSecret: `JarvisSecret(key, description, scope, value_type, required=True, is_sensitive=True, friendly_name=None)`
- CommandExample: `CommandExample(voice_command, expected_parameters, is_primary=False)`

### Code
- The run() method signature MUST be: `def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:`
  - Note the **double asterisk** on kwargs — `**kwargs` NOT `kwargs`
  - Missing ** will cause a TypeError at runtime
- Import from jarvis_command_sdk (NOT core.*)
- Include the JarvisLogger fallback pattern for logging
- run() must return CommandResponse — never raise exceptions
- context_data["message"] is what gets spoken aloud by TTS
- Use CommandResponse.final_response() when no follow-up is expected
- Use CommandResponse.success_response() when follow-up conversation is possible
- generate_prompt_examples() should have 3-5 examples
- generate_adapter_examples() should have 10-20 examples

### Dependencies
- If the command needs pip packages, declare them BOTH in `required_packages` property AND in the manifest `packages` list
- If the command needs API keys, declare them as JarvisSecret in `required_secrets` AND in the manifest `secrets` list

### README
- Every package MUST include a README.md — it is validated by the Pantry pipeline
- README must include: package description, setup instructions (secrets, API keys), usage examples (voice commands), and supported actions

### Example Manifest (use this exact structure)
```yaml
schema_version: 1
name: tell_dad_joke
display_name: Dad Jokes
description: Tells random dad jokes
version: 0.1.0
author:
  github: community
keywords:
  - joke
  - funny
categories:
  - entertainment
platforms: []
parameters: []
secrets: []
packages:
  - name: requests
    version: ">=2.28.0"
min_jarvis_version: "0.9.0"
license: MIT
```
"""


async def generate_package(
    description: str,
    llm_api_key: str,
    model_id: str = "sonnet",
    conversation_history: list[dict[str, str]] | None = None,
) -> ForgeResult:
    """Generate a Jarvis package from a natural language description.

    Args:
        description: What the user wants the command to do.
        llm_api_key: User's API key (used once, not stored).
        model_id: Model ID from LLM_MODELS registry.
        conversation_history: Optional prior messages for iterative refinement.

    Returns:
        ForgeResult with generated files and explanation.
    """
    model = LLM_MODELS.get(model_id)
    if not model:
        raise ValueError(f"Unknown model: {model_id}. Available: {', '.join(LLM_MODELS.keys())}")

    system_prompt = _build_system_prompt()

    messages: list[dict[str, str]] = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": description})

    logger.info("Forge generating package, model=%s (%s), prompt_len=%d", model_id, model.api_model, len(system_prompt))

    if model.provider == "anthropic":
        raw = await call_claude(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai" and model.api_type == "responses":
        raw = await call_openai_responses(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai":
        raw = await call_openai(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    else:
        raise ValueError(f"Unsupported provider: {model.provider}")

    logger.info("Forge LLM response received, len=%d", len(raw))
    result = _parse_forge_response(raw)

    # Run validation on generated files (if there are any — skip for Q&A responses)
    if result.files:
        result.validation = _validate_generated_files(result.files)
        if result.validation.errors:
            logger.warning("Forge validation found errors: %s", result.validation.errors)

    return result


def _validate_generated_files(files: list[GeneratedFile]) -> ForgeValidation:
    """Run static analysis on generated files without writing to disk.

    Uses the Pantry's static analysis module to check for:
    - Syntax errors
    - Missing required methods
    - Dangerous patterns
    - Manifest validation
    """
    import tempfile
    import shutil
    from pathlib import Path

    errors: list[str] = []
    warnings: list[str] = []
    dangerous: list[str] = []

    # Write files to a temp dir so static analysis can run
    tmpdir = Path(tempfile.mkdtemp(prefix="forge-validate-"))
    try:
        for f in files:
            filepath = tmpdir / f.filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(f.content, encoding="utf-8")

        # Run the Pantry's static analysis
        from .static_analysis import run_static_analysis
        result = run_static_analysis(tmpdir)
        errors.extend(result.errors)
        warnings.extend(result.warnings)
        dangerous.extend(result.dangerous_patterns)

    except Exception as e:
        logger.error("Forge validation failed: %s", e)
        warnings.append(f"Validation could not complete: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return ForgeValidation(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        dangerous_patterns=dangerous,
    )


def _parse_forge_response(text: str) -> ForgeResult:
    """Parse the JSON response from the LLM."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse Forge response: {e}\nRaw: {text[:500]}")

    files = [
        GeneratedFile(
            filename=f["filename"],
            content=f["content"],
            language=f.get("language", "text"),
        )
        for f in parsed.get("files", [])
    ]

    return ForgeResult(
        files=files,
        explanation=parsed.get("explanation", ""),
        package_name=parsed.get("package_name", "unknown"),
        display_name=parsed.get("display_name", "Unknown"),
    )
