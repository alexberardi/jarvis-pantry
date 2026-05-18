"""Resolve a submission's ``packages: [...]`` list into a frozen lockfile (#21).

Invokes ``uv pip compile`` synchronously at submission acceptance and returns
a string lockfile (pinned, with hashes). The runner-side workflow installs
exactly what's in the lockfile — no live PyPI resolution at run time.

Raises ``LockfileResolutionError`` on subprocess non-zero exit or timeout, and
``LockfileTooLargeError`` when the resolved output exceeds the size cap.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable

# GHA ``workflow_dispatch`` total input size is ~64KB. Reserve ~14KB for other
# inputs (submission_id, callback URL, nonce, future fields); cap the lockfile
# at 50KB to leave headroom. Exclusive (i.e. exactly 50KB passes).
LOCKFILE_SIZE_CAP_BYTES = 50 * 1024

# Bound on the subprocess. Real-world resolves take 1–5s with a warm cache;
# a hung subprocess should not wedge the API path.
_RESOLVER_TIMEOUT_SECONDS = 30


class LockfileResolutionError(RuntimeError):
    """``uv pip compile`` exited non-zero or hit a fatal error (timeout, missing binary)."""


class LockfileTooLargeError(RuntimeError):
    """Resolved lockfile exceeds ``LOCKFILE_SIZE_CAP_BYTES``."""


def resolve_lockfile(packages: Iterable[str]) -> str:
    """Return a frozen lockfile string for the given package names.

    Empty input returns an empty string and skips the subprocess entirely.
    """
    pkg_list = [p for p in packages if p]
    if not pkg_list:
        return ""

    cmd = [
        "uv", "pip", "compile",
        "--generate-hashes",
        "--no-header",
        "-",  # read requirements from stdin
    ]
    requirements_stdin = "\n".join(pkg_list) + "\n"

    try:
        proc = subprocess.run(
            cmd,
            input=requirements_stdin,
            capture_output=True,
            text=True,
            timeout=_RESOLVER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise LockfileResolutionError(
            f"uv pip compile timed out after {_RESOLVER_TIMEOUT_SECONDS}s",
        ) from e

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise LockfileResolutionError(
            stderr or f"uv pip compile exited {proc.returncode}",
        )

    lockfile = proc.stdout or ""
    if len(lockfile) > LOCKFILE_SIZE_CAP_BYTES:
        raise LockfileTooLargeError(
            f"Resolved lockfile is {len(lockfile)} bytes, "
            f"exceeds {LOCKFILE_SIZE_CAP_BYTES} byte cap",
        )
    return lockfile


def lockfile_to_package_specs(lockfile_content: str) -> list[str]:
    """Parse a lockfile string into a list of installable pip specs.

    Used by ``LocalRunner`` (Docker-on-host) which builds a Dockerfile with
    explicit ``pip install <spec>`` lines. The GitHub Actions runner gets the
    raw lockfile content and writes it to a file for ``pip install -r``.

    Strips hash continuation lines and comments; keeps the ``name==version``
    spec on the leading line of each entry.
    """
    specs: list[str] = []
    for raw_line in (lockfile_content or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Hash continuation lines start with `--hash=` (often after a backslash
        # join). Drop them — pip will install the leading spec without hashes
        # when the runner doesn't pass --require-hashes.
        if line.startswith("--hash"):
            continue
        # Split off any trailing space-separated --hash args
        spec = line.split(" ")[0].rstrip("\\").strip()
        if spec and not spec.startswith("--"):
            specs.append(spec)
    return specs
