#!/usr/bin/env python3
"""Bulk-submit GitHub repos to the pantry validation pipeline.

Reads a text file of `owner/repo` lines (one per line, `#` comments and blanks
ignored) and POSTs them to the operator bulk endpoint. Polls batch status
until every submission lands in a terminal state.

Usage:
    PANTRY_URL=https://pantry.example.com \\
    ADMIN_API_KEY=... \\
    LLM_API_KEY=sk-ant-... \\
    python scripts/bulk_submit.py --file repos.txt

Or with flags:
    python scripts/bulk_submit.py \\
        --pantry-url http://localhost:7721 \\
        --admin-key "$ADMIN_API_KEY" \\
        --llm-key "$ANTHROPIC_API_KEY" \\
        --file repos.txt

Pair with `gh repo list <owner> --json nameWithOwner -q '.[].nameWithOwner'`
to feed your full repo set in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


TERMINAL_STATUSES = {"published", "rejected", "callback_timeout"}


def parse_repo_file(path: Path) -> list[str]:
    """One `owner/repo` per line. `#` comments and blanks skipped."""
    repos: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("https://github.com/"):
            line = line[len("https://github.com/"):].rstrip("/")
        repos.append(line)
    return repos


def submit_batch(
    *,
    pantry_url: str,
    admin_key: str,
    llm_provider: str,
    llm_key: str,
    repos: list[str],
    timeout: float,
) -> dict:
    payload = {
        "repos": [{"owner_repo": r} for r in repos],
        "llm_provider": llm_provider,
        "llm_api_key": llm_key,
    }
    resp = httpx.post(
        f"{pantry_url}/v1/admin/bulk-submissions",
        json=payload,
        headers={"X-Admin-Key": admin_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_status(*, pantry_url: str, admin_key: str, batch_id: str) -> dict:
    resp = httpx.get(
        f"{pantry_url}/v1/admin/bulk-submissions/{batch_id}",
        headers={"X-Admin-Key": admin_key},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def print_summary(status: dict) -> None:
    """One-line summary + per-row state."""
    by = status.get("by_status", {})
    parts = [f"{k}={v}" for k, v in sorted(by.items())]
    print(f"\nbatch {status['batch_id']}  total={status['total']}  " + " ".join(parts))
    for s in status.get("submissions", []):
        cmd = s.get("command_name") or "—"
        err = s.get("error_message") or ""
        run = s.get("external_run_url") or ""
        repo = s["github_repo_url"].replace("https://github.com/", "")
        line = f"  [{s['status']:<22}] {repo:<40} {cmd}"
        if run:
            line += f"  {run}"
        if err:
            # Trim noisy multi-line errors to one line for the console.
            err_short = err.replace("\n", " ")[:140]
            line += f"\n      ↳ {err_short}"
        print(line)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", "-f", required=True, type=Path, help="Path to a text file of owner/repo lines.")
    p.add_argument("--pantry-url", default=os.getenv("PANTRY_URL", "http://localhost:7721"))
    p.add_argument("--admin-key", default=os.getenv("ADMIN_API_KEY", ""))
    p.add_argument("--llm-provider", default=os.getenv("PANTRY_LLM_PROVIDER", "claude"), choices=["claude", "openai"])
    p.add_argument("--llm-key", default=os.getenv("LLM_API_KEY", ""))
    p.add_argument("--poll-interval", type=float, default=10.0)
    p.add_argument("--no-poll", action="store_true", help="Submit and exit without polling.")
    p.add_argument("--json", action="store_true", help="Emit JSON status to stdout instead of human-readable.")
    p.add_argument("--submit-timeout", type=float, default=600.0, help="HTTP timeout for the submit POST (default 600s).")
    args = p.parse_args()

    if not args.admin_key:
        sys.stderr.write("error: --admin-key or ADMIN_API_KEY required\n")
        return 2
    if not args.llm_key:
        sys.stderr.write("error: --llm-key or LLM_API_KEY required\n")
        return 2
    if not args.file.is_file():
        sys.stderr.write(f"error: file not found: {args.file}\n")
        return 2

    repos = parse_repo_file(args.file)
    if not repos:
        sys.stderr.write("error: no repos in file\n")
        return 2

    pantry_url = args.pantry_url.rstrip("/")
    print(f"Submitting {len(repos)} repo(s) to {pantry_url} …", file=sys.stderr)
    try:
        result = submit_batch(
            pantry_url=pantry_url,
            admin_key=args.admin_key,
            llm_provider=args.llm_provider,
            llm_key=args.llm_key,
            repos=repos,
            timeout=args.submit_timeout,
        )
    except httpx.HTTPStatusError as e:
        sys.stderr.write(f"submit failed: HTTP {e.response.status_code}: {e.response.text}\n")
        return 1
    except httpx.HTTPError as e:
        sys.stderr.write(f"submit failed: {e}\n")
        return 1

    batch_id = result["batch_id"]
    print(
        f"batch_id={batch_id}  accepted={result['accepted_count']}  rejected={result['rejected_count']}",
        file=sys.stderr,
    )
    for o in result["outcomes"]:
        flag = "✓" if o["accepted"] else "✗"
        reason = f"  {o['reason']}" if o.get("reason") else ""
        print(f"  {flag} {o['owner_repo']} → submission_id={o['submission_id']}{reason}", file=sys.stderr)

    if args.no_poll:
        if args.json:
            print(json.dumps(result, indent=2))
        return 0

    print("\nPolling status (Ctrl+C to stop) …", file=sys.stderr)
    while True:
        try:
            status = fetch_status(pantry_url=pantry_url, admin_key=args.admin_key, batch_id=batch_id)
        except httpx.HTTPError as e:
            sys.stderr.write(f"status fetch failed: {e}\n")
            time.sleep(args.poll_interval)
            continue

        if not args.json:
            print_summary(status)

        all_done = all(s["status"] in TERMINAL_STATUSES for s in status["submissions"])
        if all_done:
            break
        time.sleep(args.poll_interval)

    if args.json:
        print(json.dumps(status, indent=2))

    failed = sum(1 for s in status["submissions"] if s["status"] != "published")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
