"""Apt-package allow-list loader (#16).

Reads ``config/apt-allowlist.yaml`` and exposes membership lookups for the
static-analysis hook in ``static_analysis._validate_apt_allowlist``.

The loader is fail-closed: a missing or malformed file raises rather than
silently producing an empty allow-list (which would let every off-list
package through and defeat the safety gate).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / "config" / "apt-allowlist.yaml"


class AptAllowlistLoadError(Exception):
    """Raised when the allow-list YAML cannot be parsed or is structurally invalid."""


@dataclass(frozen=True)
class AptAllowlistEntry:
    name: str
    reason: str
    added_by: str
    added_at: str


@dataclass
class AptAllowlist:
    entries: list[AptAllowlistEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        index: dict[str, AptAllowlistEntry] = {}
        for entry in self.entries:
            index.setdefault(entry.name, entry)
        self._index = index

    def is_allowed(self, name: str) -> bool:
        return name in self._index

    def find(self, name: str) -> AptAllowlistEntry | None:
        return self._index.get(name)


def load_allowlist(path: Path) -> AptAllowlist:
    """Load the allow-list from a YAML file. Fail-closed on any structural problem."""
    if not path.exists():
        raise FileNotFoundError(f"apt allow-list config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise AptAllowlistLoadError(f"malformed YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise AptAllowlistLoadError(f"{path}: top-level must be a mapping, got {type(raw).__name__}")

    packages = raw.get("packages")
    if not isinstance(packages, list):
        raise AptAllowlistLoadError(f"{path}: 'packages' must be a list, got {type(packages).__name__}")

    entries: list[AptAllowlistEntry] = []
    for i, item in enumerate(packages):
        if not isinstance(item, dict):
            raise AptAllowlistLoadError(f"{path}: entry #{i} must be a mapping, got {type(item).__name__}")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise AptAllowlistLoadError(f"{path}: entry #{i} missing required 'name' field")
        entries.append(AptAllowlistEntry(
            name=name,
            reason=str(item.get("reason", "")),
            added_by=str(item.get("added_by", "")),
            added_at=str(item.get("added_at", "")),
        ))

    return AptAllowlist(entries=entries)


_cached: AptAllowlist | None = None


def get_allowlist() -> AptAllowlist:
    """Return the process-wide cached allow-list loaded from the default config path."""
    global _cached
    if _cached is None:
        _cached = load_allowlist(DEFAULT_ALLOWLIST_PATH)
    return _cached


def _reset_cache_for_testing() -> None:
    """Drop the cached allow-list. Used only by tests that mutate the config file."""
    global _cached
    _cached = None


def request_url_for(package: str) -> str:
    """Build a one-click GH issue URL prefilled to request a specific apt package."""
    from urllib.parse import quote
    title = quote(f"Add {package} to apt-allowlist")
    return (
        "https://github.com/alexberardi/jarvis-pantry/issues/new"
        f"?template=apt-package-request.yaml&title={title}"
    )
