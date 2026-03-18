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

Evaluate these dimensions:
1. **Network access** — Does it make network calls? Are URLs hardcoded or configurable? Suspicious endpoints?
2. **Filesystem access** — Does it read/write outside its own directory? Access system files?
3. **Secret usage** — Does it only access secrets it declares? Does it try to read other secrets?
4. **Code execution** — Does it use subprocess, eval, exec, os.system, or compile?
5. **Obfuscation** — Is there base64-encoded data, encoded bytes, or minified code?
6. **Resource usage** — Are there infinite loops, large allocations, or sleep-based DoS?

Danger scale:
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

Command source code:
```python
%s
```
"""


@dataclass
class SecurityReviewResult:
    danger_score: int
    summary: str
    concerns: list[str]
    recommendation: str  # approve, flag_for_review, reject
    raw_response: dict[str, Any]


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
