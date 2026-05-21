"""Apt-source allow-list loader.

Reads ``config/apt-source-allowlist.yaml`` and exposes membership lookups
for the static-analysis hook in ``static_analysis._validate_apt_source_allowlist``.

Mirrors ``apt_allowlist`` for 3rd-party apt repository sources (key URL +
``deb`` repo line) that packages declare in their ``apt_sources:`` manifest
field. Allowed entries must match the manifest's ``(name, key_url, repo)``
triple exactly; near-misses are rejected. The loader is fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / "config" / "apt-source-allowlist.yaml"


class AptSourceAllowlistLoadError(Exception):
    """Raised when the allow-list YAML cannot be parsed or is structurally invalid."""


@dataclass(frozen=True)
class AptSourceAllowlistEntry:
    name: str
    key_url: str
    repo: str
    reason: str
    added_by: str
    added_at: str


@dataclass
class AptSourceAllowlist:
    entries: list[AptSourceAllowlistEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        index: dict[str, AptSourceAllowlistEntry] = {}
        for entry in self.entries:
            index.setdefault(entry.name, entry)
        self._index = index

    def find(self, name: str) -> AptSourceAllowlistEntry | None:
        return self._index.get(name)

    def matches(self, name: str, key_url: str, repo: str) -> bool:
        """Exact-match check against (name, key_url, repo).

        Used by static analysis: a manifest-declared source must match an
        allowlist entry in all three fields. A typo'd or substituted URL
        fails this check even if the name is known.
        """
        entry = self._index.get(name)
        if entry is None:
            return False
        return entry.key_url == key_url and entry.repo == repo


def load_allowlist(path: Path) -> AptSourceAllowlist:
    """Load the allow-list from a YAML file. Fail-closed on any structural problem."""
    if not path.exists():
        raise FileNotFoundError(f"apt-source allow-list config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise AptSourceAllowlistLoadError(f"malformed YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise AptSourceAllowlistLoadError(
            f"{path}: top-level must be a mapping, got {type(raw).__name__}"
        )

    sources = raw.get("sources")
    if not isinstance(sources, list):
        raise AptSourceAllowlistLoadError(
            f"{path}: 'sources' must be a list, got {type(sources).__name__}"
        )

    entries: list[AptSourceAllowlistEntry] = []
    for i, item in enumerate(sources):
        if not isinstance(item, dict):
            raise AptSourceAllowlistLoadError(
                f"{path}: entry #{i} must be a mapping, got {type(item).__name__}"
            )
        name = item.get("name")
        key_url = item.get("key_url")
        repo = item.get("repo")
        if not isinstance(name, str) or not name:
            raise AptSourceAllowlistLoadError(f"{path}: entry #{i} missing 'name'")
        if not isinstance(key_url, str) or not key_url:
            raise AptSourceAllowlistLoadError(f"{path}: entry '{name}' missing 'key_url'")
        if not isinstance(repo, str) or not repo:
            raise AptSourceAllowlistLoadError(f"{path}: entry '{name}' missing 'repo'")
        entries.append(AptSourceAllowlistEntry(
            name=name,
            key_url=key_url,
            repo=repo,
            reason=str(item.get("reason", "")),
            added_by=str(item.get("added_by", "")),
            added_at=str(item.get("added_at", "")),
        ))

    return AptSourceAllowlist(entries=entries)


_cached: AptSourceAllowlist | None = None


def get_allowlist() -> AptSourceAllowlist:
    """Return the process-wide cached allow-list loaded from the default config path."""
    global _cached
    if _cached is None:
        _cached = load_allowlist(DEFAULT_ALLOWLIST_PATH)
    return _cached


def _reset_cache_for_testing() -> None:
    """Drop the cached allow-list. Used only by tests that mutate the config file."""
    global _cached
    _cached = None


def request_url_for(source_name: str) -> str:
    """Build a one-click GH issue URL prefilled to request a specific apt source."""
    from urllib.parse import quote
    title = quote(f"Add {source_name} to apt-source-allowlist")
    return (
        "https://github.com/alexberardi/jarvis-pantry/issues/new"
        f"?template=apt-source-request.yaml&title={title}"
    )
