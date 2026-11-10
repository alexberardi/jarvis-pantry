"""AI-powered package generator for the Forge.

Takes a natural language description of what the user wants, fetches the
SDK authoring spec, and calls an LLM to generate the complete package
(command.py, jarvis_command.yaml, README.md).

Uses BYOK (bring your own key) — same pattern as security_review.py.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .llm_client import (
    LLM_MODELS,
    call_claude,
    call_local,
    call_openai,
    call_openai_responses,
    resolve_local_model,
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


_LANGUAGE_BY_EXTENSION = {".py": "python", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".json": "json"}


def language_for(filename: str, claimed: str | None) -> str:
    """Highlighting language from the file extension; models mislabel files when revising (every file came back "text")."""
    for ext, language in _LANGUAGE_BY_EXTENSION.items():
        if filename.lower().endswith(ext):
            return language
    return claimed or "text"


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


_PACKAGE_TOOLS = """
You can search the published packages in the Pantry and read their files with your tools \
(search_packages, list_package_files, read_package_file). When an existing package is a useful \
reference, look at it yourself instead of asking the user to paste its code: for example, to \
build a bridge-style device protocol, read how `zwave` (a device protocol that talks to Z-Wave \
JS UI) is put together. Read only what you need, then write the reply in the output format below.
"""


def _build_system_prompt(tools: bool = False) -> str:
    """Build the Forge system prompt from the SDK spec. ``tools``: the caller gives the model the package
    reading tools (the browser path does; the server path for local models has none)."""
    # Imported lazily: the SDK is only needed to actually generate a package
    # (it's installed in the deployed image), so the module stays importable
    # for validation/tests without jarvis_command_sdk present.
    from jarvis_command_sdk.forge import generate_spec_markdown

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

When asking questions, reply in the output format below with only an EXPLANATION \
section holding your questions, and no FILE sections. The user will answer and you can \
then generate the full package.

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

Do not write a LICENSE file: Pantry adds the MIT license to every package.
{_PACKAGE_TOOLS if tools else ""}
## Output Format

Reply in this plain-text format, with no JSON and no markdown fence around the reply. Each \
file's content goes between its marker and the next marker exactly as it should be saved:

=== PACKAGE ===
package_name: snake_case_name
display_name: Human Readable Name
=== EXPLANATION ===
Brief explanation of what was generated, or changed, and how to use it.
=== FILE: command.py ===
...the file's full content...
=== FILE: jarvis_command.yaml ===
...the file's full content...
=== END ===

When revising an existing package (the user's message includes its current files), write \
ONLY the files you add or change, each in full; files you leave out are kept as they are. \
To remove a file, add a line `=== DELETE: path ===`. Always finish with `=== END ===`.

## Critical Rules (violations will fail validation)

### Naming
- command_name MUST be snake_case (e.g., "get_weather", "tell_dad_joke")
- The manifest `name` field MUST exactly match the command_name property in command.py
- The PACKAGE section's `package_name` MUST also match both of these

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
- Read a secret's value with the SDK's storage, the same way for every component type: \
`from jarvis_command_sdk import JarvisStorage`, then `JarvisStorage("<package_name>").get_secret("KEY")` \
(scope "integration" by default; pass `"user"` for user-scoped secrets). It returns None when unset. Read it \
when needed, not once at import, since the user may set it later. NEVER import from `services.*`, `core.*` \
or `utils.*`: those are the old Python node's internals and don't exist on current nodes.

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
    local_config=None,
    local_models: list[str] | None = None,
    current_files: list[GeneratedFile] | None = None,
    package_name: str = "",
    display_name: str = "",
) -> ForgeReply:
    """Generate a Jarvis package from a natural language description.

    Args:
        description: What the user wants the command to do.
        llm_api_key: User's API key (used once, not stored).
        model_id: Model ID from LLM_MODELS registry, or ``local:<model>`` for the configured local server.
        local_config: LocalLLMConfig (base URL, key, default model) for ``local:`` models.
        local_models: Model ids last detected on the local server, used to resolve a bare ``local``.
        conversation_history: Optional prior messages for iterative refinement.

    Returns:
        ForgeReply: the whole package after the reply, and the files it changed or deleted.
    """
    model = LLM_MODELS.get(model_id)
    if not model and local_config is not None and local_config.configured:
        model = resolve_local_model(model_id, local_config.default_model, local_models)
    if not model:
        raise ValueError(f"Unknown model: {model_id}. Available: {', '.join(LLM_MODELS.keys())}")

    system_prompt = _build_system_prompt()

    messages: list[dict[str, str]] = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": description})

    logger.info("Forge generating package, model=%s (%s), prompt_len=%d", model_id, model.api_model, len(system_prompt))

    if model.provider == "local":
        raw = await call_local(
            system_prompt, messages, local_config.api_key,  # never the caller's key: it may be a hosted provider's
            model.api_model, model.max_tokens, local_config.base_url,
        )
    elif model.provider == "anthropic":
        raw = await call_claude(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai" and model.api_type == "responses":
        raw = await call_openai_responses(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    elif model.provider == "openai":
        raw = await call_openai(system_prompt, messages, llm_api_key, model.api_model, model.max_tokens)
    else:
        raise ValueError(f"Unsupported provider: {model.provider}")

    logger.info("Forge LLM response received, len=%d", len(raw))
    reply = merge_forge_reply(raw, current_files, package_name, display_name)
    if reply.result.validation and reply.result.validation.errors:
        logger.warning("Forge validation found errors: %s", reply.result.validation.errors)
    return reply


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
    tmpdir_resolved = tmpdir.resolve()
    try:
        for f in files:
            filepath = (tmpdir / f.filename).resolve()
            # The filename is chosen by the (untrusted) LLM. Reject absolute
            # paths and ".." traversal that would escape the temp dir — an
            # unsanitized join here is an arbitrary-file-write on the host.
            try:
                filepath.relative_to(tmpdir_resolved)
            except ValueError:
                msg = f"unsafe filename rejected: {f.filename!r}"
                errors.append(msg)
                dangerous.append(msg)
                continue
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


_MARKER = re.compile(r"^=== (PACKAGE|EXPLANATION|END|FILE: (?P<file>.+?)|DELETE: (?P<delete>.+?)) ===[ \t]*$", re.M)

MIT_LICENSE = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


@dataclass
class ForgeReply:
    """A reply merged into the package it revises: the whole package, and what the reply touched."""

    result: ForgeResult
    changed: list[str]
    deleted: list[str]
    complete: bool  # the reply reached === END ===; False means it was cut off


def merge_forge_reply(
    text: str,
    current_files: list[GeneratedFile] | None = None,
    package_name: str = "",
    display_name: str = "",
) -> ForgeReply:
    """Parse a reply in the text format (or the older JSON one) and apply it to the current files:
    its FILE sections replace or add files, DELETE lines remove them, everything else is kept.
    A package gets Pantry's MIT LICENSE when it has none."""
    files = {f.filename: f for f in current_files or []}
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("```"):
        old = _parse_forge_response(text)
        changed = [f.filename for f in old.files]
        for f in old.files:
            files[f.filename] = f
        explanation, deleted, complete = old.explanation, [], True
        package_name = old.package_name if old.package_name not in ("", "unknown") else package_name
        display_name = old.display_name if old.display_name not in ("", "Unknown") else display_name
    else:
        explanation, changed, deleted, complete = "", [], [], False
        marks = list(_MARKER.finditer(text))
        if not marks:  # no format at all: treat the reply as an answer to the user
            explanation = stripped
        for i, m in enumerate(marks):
            body_end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():body_end]
            body = body[1:] if body.startswith("\n") else body
            kind = m.group(1)
            if kind == "END":
                complete = True
            elif kind == "PACKAGE":
                for line in body.splitlines():
                    key, _, value = line.partition(":")
                    if key.strip() == "package_name" and value.strip():
                        package_name = value.strip()
                    elif key.strip() == "display_name" and value.strip():
                        display_name = value.strip()
            elif kind == "EXPLANATION":
                explanation = body.strip()
            elif m.group("delete"):
                name = m.group("delete").strip()
                deleted.append(name)
                files.pop(name, None)
            elif m.group("file"):
                name = m.group("file").strip()
                content = body[:-1] if body.endswith("\n") else body
                files[name] = GeneratedFile(filename=name, content=content, language=language_for(name, None))
                if name not in changed:
                    changed.append(name)
    if files and "LICENSE" not in files:
        from datetime import date
        files["LICENSE"] = GeneratedFile(
            filename="LICENSE", content=MIT_LICENSE.format(year=date.today().year, holder="Jarvis community"),
            language="text",
        )
    result = ForgeResult(
        files=list(files.values()), explanation=explanation,
        package_name=package_name or "unknown", display_name=display_name or "Unknown",
    )
    if changed or deleted:
        result.validation = _validate_generated_files(result.files)
    return ForgeReply(result=result, changed=changed, deleted=deleted, complete=complete)


def _parse_forge_response(text: str) -> ForgeResult:
    """Parse the JSON response from the LLM (the format before the text one)."""
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
            language=language_for(f["filename"], f.get("language")),
        )
        for f in parsed.get("files", [])
    ]

    return ForgeResult(
        files=files,
        explanation=parsed.get("explanation", ""),
        package_name=parsed.get("package_name", "unknown"),
        display_name=parsed.get("display_name", "Unknown"),
    )
