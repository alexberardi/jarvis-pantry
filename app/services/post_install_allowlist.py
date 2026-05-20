"""Post-install op allow-list loader (#19).

Reads ``config/post-install-allowlist.yaml`` and exposes membership lookups
for the static-analysis hook in
``static_analysis._validate_post_install_allowlist``.

The file is structured as a mapping of op type → list of allowed targets.
For ``configure_systemd_service`` the target key is ``service``; for
``set_config_file_value`` it's ``file``. New op types extend the union by
adding an entry to ``_TARGET_KEYS_BY_OP`` here and to
``forge.MANIFEST_SCHEMA`` in the SDK.

Fail-closed: a missing or malformed file raises rather than silently
producing an empty allow-list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / "config" / "post-install-allowlist.yaml"


# Per-op-type: which manifest field on the op dict identifies the *target*
# resource that the allow-list gates. Keep this in sync with the named ops
# documented in jarvis-command-sdk forge.MANIFEST_SCHEMA["post_install"].
_TARGET_KEYS_BY_OP: dict[str, str] = {
    "configure_systemd_service": "service",
    "set_config_file_value": "file",
}


class PostInstallAllowlistLoadError(Exception):
    """Raised when the allow-list YAML cannot be parsed or is structurally invalid."""


@dataclass(frozen=True)
class PostInstallAllowlistEntry:
    op_type: str
    target: str
    reason: str
    added_by: str
    added_at: str


@dataclass
class PostInstallAllowlist:
    entries: list[PostInstallAllowlistEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        # (op_type, target) → entry
        index: dict[tuple[str, str], PostInstallAllowlistEntry] = {}
        for entry in self.entries:
            index.setdefault((entry.op_type, entry.target), entry)
        self._index = index

    def known_op_types(self) -> set[str]:
        return set(_TARGET_KEYS_BY_OP.keys())

    def target_key_for(self, op_type: str) -> str | None:
        """Which manifest field identifies the target resource for this op type."""
        return _TARGET_KEYS_BY_OP.get(op_type)

    def is_allowed(self, op_type: str, target: str) -> bool:
        return (op_type, target) in self._index

    def find(self, op_type: str, target: str) -> PostInstallAllowlistEntry | None:
        return self._index.get((op_type, target))


def load_allowlist(path: Path) -> PostInstallAllowlist:
    """Load the allow-list from a YAML file. Fail-closed on any structural problem."""
    if not path.exists():
        raise FileNotFoundError(f"post-install allow-list config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise PostInstallAllowlistLoadError(f"malformed YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise PostInstallAllowlistLoadError(
            f"{path}: top-level must be a mapping, got {type(raw).__name__}",
        )

    entries: list[PostInstallAllowlistEntry] = []
    for op_type, op_entries in raw.items():
        if op_type not in _TARGET_KEYS_BY_OP:
            raise PostInstallAllowlistLoadError(
                f"{path}: unknown op type {op_type!r}. "
                f"Known: {sorted(_TARGET_KEYS_BY_OP.keys())}",
            )
        target_key = _TARGET_KEYS_BY_OP[op_type]
        if not isinstance(op_entries, list):
            raise PostInstallAllowlistLoadError(
                f"{path}: '{op_type}' must be a list, got {type(op_entries).__name__}",
            )
        for i, item in enumerate(op_entries):
            if not isinstance(item, dict):
                raise PostInstallAllowlistLoadError(
                    f"{path}: {op_type}[{i}] must be a mapping, "
                    f"got {type(item).__name__}",
                )
            target = item.get(target_key)
            if not isinstance(target, str) or not target:
                raise PostInstallAllowlistLoadError(
                    f"{path}: {op_type}[{i}] missing required {target_key!r}",
                )
            entries.append(PostInstallAllowlistEntry(
                op_type=op_type,
                target=target,
                reason=str(item.get("reason", "")),
                added_by=str(item.get("added_by", "")),
                added_at=str(item.get("added_at", "")),
            ))

    return PostInstallAllowlist(entries=entries)


_cached: PostInstallAllowlist | None = None


def get_allowlist() -> PostInstallAllowlist:
    """Return the process-wide cached allow-list loaded from the default path."""
    global _cached
    if _cached is None:
        _cached = load_allowlist(DEFAULT_ALLOWLIST_PATH)
    return _cached


def _reset_cache_for_testing() -> None:
    """Drop the cached allow-list. Used only by tests that mutate the config file."""
    global _cached
    _cached = None


def request_url_for(op_type: str, target: str) -> str:
    """Build a one-click GH issue URL prefilled to request a specific op + target."""
    from urllib.parse import quote
    title = quote(f"Allow post_install op '{op_type}' for '{target}'")
    return (
        "https://github.com/alexberardi/jarvis-pantry/issues/new"
        f"?template=post-install-request.yaml&title={title}"
    )
