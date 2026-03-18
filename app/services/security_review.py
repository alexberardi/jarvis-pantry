"""AI security review using the submitter's API key (BYOK).

The store controls the prompt and API URL — the submitter only provides
their API key and preferred provider. The key is used for one call and
never stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


SECURITY_REVIEW_PROMPT = """\
You are a security auditor for a voice assistant command plugin system.

Analyze the following Python command plugin code for security risks.
Commands run on user-owned devices (Raspberry Pi / macOS) with access to
a local secrets database, network, and filesystem.

## Plugin Interface Reference

Commands subclass `IJarvisCommand` from `jarvis_command_sdk`. Here is the
expected interface — anything that deviates is suspicious:

**Required properties/methods (normal to see):**
- `command_name` (property) → str: unique identifier
- `description` (property) → str: human-readable description
- `parameters` (property) → list[JarvisParameter]: declared input parameters
- `required_secrets` (property) → list[JarvisSecret]: declared secrets this command needs
- `keywords` (property) → list[str]: fuzzy-match keywords
- `run(request_info: RequestInformation, **kwargs) → CommandResponse | dict`: core execution
- `generate_prompt_examples() → list[CommandExample]`: voice command examples
- `generate_adapter_examples() → list[CommandExample]`: training examples

**Optional overrides (normal to see):**
- `validate_call(**kwargs) → list[ValidationResult]`: parameter validation
- `pre_route(voice_command: str) → PreRouteResult | None`: deterministic matching
- `post_process_tool_call(args, voice_command) → dict`: fix LLM tool-call args
- `authentication` (property) → AuthenticationConfig | None: OAuth config
- `supported_platforms` (property) → list[str]: e.g. ["darwin"]
- `store_auth_values(values: dict) → None`: called when OAuth tokens arrive
- `handle_action(action_name, context) → CommandResponse`: handle mobile button taps

**How secrets work:**
- Commands DECLARE needed secrets via `required_secrets` (key, scope, description).
- At runtime, the node framework provides secrets to `run()` — the SDK itself has
  NO `get_secret()` method. If you see direct file reads of secrets databases,
  environment variable access for API keys, or attempts to read secrets not
  declared in `required_secrets`, that is suspicious.

**RequestInformation fields:** `voice_command` (str), `conversation_id` (str),
`is_validation_response` (bool), `validation_context` (dict | None).

**CommandResponse fields:** `context_data` (dict), `success` (bool),
`error_details` (str | None), `wait_for_input` (bool), `actions` (list of buttons).

**How data persistence works:**
- Commands that need to store state MUST use the `CommandDataRepository` from
  the node runtime. It provides scoped CRUD: `save(command_name, data_key, data)`,
  `get(command_name, data_key)`, `get_all(command_name)`, `delete(command_name, data_key)`.
- Data is keyed by `command_name`, so each command can only access its own data.
- The correct import pattern is: `from db import SessionLocal` and
  `from repositories.command_data_repository import CommandDataRepository`.
- Commands should NEVER import raw database libraries (sqlite3, sqlalchemy,
  psycopg2, etc.) or write raw SQL. If they need persistence, they use
  `CommandDataRepository`.

## Evaluation Dimensions

1. **Network access** — Does it make network calls? Are URLs hardcoded or configurable? Suspicious endpoints?
2. **Filesystem access** — Does it read/write outside its own directory? Access system files?
3. **Secret usage** — Does it only use secrets it declares in `required_secrets`? Does it try to access env vars, read files, or import secrets it hasn't declared?
4. **Code execution** — Does it use subprocess, eval, exec, os.system, or compile?
5. **Obfuscation** — Is there base64-encoded data, encoded bytes, or minified code?
6. **Resource usage** — Are there infinite loops, large allocations, or sleep-based DoS?
7. **Database access** — Commands MUST use `CommandDataRepository` for persistence.
   Importing raw database libraries (sqlite3, sqlalchemy, alembic, psycopg2, etc.)
   is a red flag — it means the command is bypassing the data access layer.
   Schema mutations (CREATE TABLE, ALTER TABLE, DROP TABLE, migrations) should
   be rejected outright — the node manages its own schema. If a command uses
   `CommandDataRepository` but passes a different command's name to access
   another command's data, flag it as a concern — legitimate cross-command data
   access is rare and should be reviewed.

## Danger Scale

1 = Pure logic, no network/filesystem (calculator, converter)
2 = Reads from well-known API with declared key (weather, stocks)
3 = Network to user-configured endpoint, writes to filesystem
4 = Subprocess calls, dynamic code, broad network access
5 = Obfuscated code, credential access, shell execution

Respond with ONLY a JSON object (no markdown, no explanation):
{
  "danger_score": <1-5>,
  "summary": "<one sentence overall assessment>",
  "concerns": ["<specific concern 1>", "<specific concern 2>"],
  "recommendation": "<approve|flag_for_review|reject>"
}

## Additional Plugin Types

**Agents** subclass `IJarvisAgent`. They run on a background schedule, collecting
data and injecting it into voice request context. Normal to see:
- `name` (property) → str
- `description` (property) → str
- `schedule` (property) → AgentSchedule (interval_seconds, run_on_startup)
- `required_secrets` (property) → list[IJarvisSecret]
- `run()` → None: background data collection (may make network calls)
- `get_context_data()` → dict: returns cached data (should be fast)

Agents running on a schedule and caching data is normal. Suspicious: agents that
modify system state, spawn processes, or access secrets they didn't declare.

**Device protocols** subclass `IJarvisDeviceProtocol`. They handle discovery and
control for a specific device type (e.g., LIFX lights, Kasa smart plugs).
Normal to see:
- `protocol_name` (property) → str
- `supported_domains` (property) → list[str] (e.g., ["light", "switch"])
- `discover()` → discovers devices on the network
- `control(entity_id, action, params)` → sends commands to devices
- `get_state(entity_id)` → reads device state

Network access for LAN device discovery and control is normal for device protocols.
Suspicious: protocols that phone home to non-device endpoints, access unrelated
secrets, or modify the filesystem.

**Device managers** subclass `IJarvisDeviceManager`. They aggregate device
listings from backends (Home Assistant, etc.). Normal to see:
- `name` (property) → str
- `friendly_name` (property) → str
- `description` (property) → str
- `collect_devices()` → list of normalized device records

Network calls to local backends (Home Assistant, etc.) are normal. Suspicious:
calls to unknown remote endpoints or filesystem access.

Source code:
```python
%s
```
"""


# Token pricing per million tokens (as of 2026-03)
_PRICING: dict[str, dict[str, float]] = {
    "claude": {"input": 3.00, "output": 15.00},   # Claude Sonnet 4
    "openai": {"input": 2.50, "output": 10.00},    # GPT-4o
}

# Approximate prompt overhead in tokens (interface reference + evaluation dimensions)
_PROMPT_OVERHEAD_TOKENS = 1400  # increased for agent/protocol/manager interface docs
# Approximate output tokens (JSON response)
_OUTPUT_TOKENS = 250


def estimate_review_cost(source_code: str, llm_provider: str) -> dict[str, Any]:
    """Estimate the cost of an AI security review.

    Uses a rough 1 token ≈ 4 characters heuristic for the source code,
    plus fixed overhead for the prompt template and expected output.

    Returns:
        Dict with estimated_tokens, estimated_cost_usd, and formatted string.
    """
    source_tokens = len(source_code) // 4
    input_tokens = _PROMPT_OVERHEAD_TOKENS + source_tokens
    output_tokens = _OUTPUT_TOKENS

    pricing = _PRICING.get(llm_provider, _PRICING["claude"])
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    total_cost = input_cost + output_cost

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": round(total_cost, 4),
        "formatted": f"~${total_cost:.4f}" if total_cost < 0.01 else f"~${total_cost:.2f}",
    }


@dataclass
class SecurityReviewResult:
    danger_score: int
    summary: str
    concerns: list[str]
    recommendation: str  # approve, flag_for_review, reject
    raw_response: dict[str, Any]


def format_bundle_source(component_sources: dict[str, tuple[str, str]]) -> str:
    """Format multiple component sources into a single review document.

    Args:
        component_sources: Dict mapping component name to (type, source_code).

    Returns:
        Combined source string with component headers.
    """
    parts: list[str] = []
    for comp_name, (comp_type, source) in component_sources.items():
        parts.append(f"## Component: {comp_name} ({comp_type})\n\n{source}")
    return "\n\n---\n\n".join(parts)


async def run_security_review(
    source_code: str,
    llm_provider: str,
    llm_api_key: str,
) -> SecurityReviewResult:
    """Run AI security review on command source code.

    Args:
        source_code: The command.py source code.
        llm_provider: "claude" or "openai".
        llm_api_key: The submitter's API key (used once, not stored).

    Returns:
        SecurityReviewResult with danger score and analysis.
    """
    prompt = SECURITY_REVIEW_PROMPT % source_code

    if llm_provider == "claude":
        result = await _call_claude(prompt, llm_api_key)
    elif llm_provider == "openai":
        result = await _call_openai(prompt, llm_api_key)
    else:
        raise ValueError(f"Unsupported LLM provider: {llm_provider}")

    return result


async def _call_claude(prompt: str, api_key: str) -> SecurityReviewResult:
    """Call Claude API for security review."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Claude API error: {resp.status_code} — {resp.text}")

    data = resp.json()
    text = data["content"][0]["text"]
    return _parse_review_response(text, data)


async def _call_openai(prompt: str, api_key: str) -> SecurityReviewResult:
    """Call OpenAI API for security review."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60.0,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI API error: {resp.status_code} — {resp.text}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return _parse_review_response(text, data)


def _parse_review_response(text: str, raw: dict[str, Any]) -> SecurityReviewResult:
    """Parse the JSON response from the LLM."""
    import json

    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last line (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse AI review response: {e}\nRaw: {text}")

    return SecurityReviewResult(
        danger_score=int(parsed.get("danger_score", 5)),
        summary=parsed.get("summary", "No summary"),
        concerns=parsed.get("concerns", []),
        recommendation=parsed.get("recommendation", "flag_for_review"),
        raw_response=raw,
    )
