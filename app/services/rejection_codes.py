"""Structured rejection-reason taxonomy for command submissions (#18).

Every rejection / warning carries a stable ``reason_code`` (snake_case string),
optional location fields (``file`` / ``line`` / ``snippet``), rule-specific
extras (``primitive`` / ``value`` / ``message``), and a ``doc_url`` that points
back at user-facing rejection docs on docs.jarvisautomation.dev.

Catalogue is append-only: codes never get renamed once they ship, since
clients depend on them as stable wire identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# ── Reason code catalogue ────────────────────────────────────────────────
# Append-only. Never rename a shipped code.

# Static analysis (Python source-level findings)
STATIC_ANALYSIS_DISALLOWED_PRIMITIVE = "static_analysis_disallowed_primitive"
STATIC_ANALYSIS_RAW_DB_IMPORT = "static_analysis_raw_db_import"
STATIC_ANALYSIS_SQL_MUTATION = "static_analysis_sql_mutation"
STATIC_ANALYSIS_CROSS_COMMAND_ACCESS = "static_analysis_cross_command_access"
STATIC_ANALYSIS_SHADOWS_BUILTIN_DIR = "static_analysis_shadows_builtin_dir"
STATIC_ANALYSIS_SYNTAX_ERROR = "static_analysis_syntax_error"
STATIC_ANALYSIS_MISSING_BASE_CLASS = "static_analysis_missing_base_class"
STATIC_ANALYSIS_TRANSITIVE_INHERITANCE = "static_analysis_transitive_inheritance"

# Manifest validation
MANIFEST_BAD_SEMVER = "manifest_bad_semver"
MANIFEST_MISSING_REQUIRED_FIELD = "manifest_missing_required_field"
MANIFEST_INVALID_FIELD_TYPE = "manifest_invalid_field_type"
MANIFEST_UNKNOWN_CATEGORY = "manifest_unknown_category"
MANIFEST_UNKNOWN_PARAM_TYPE = "manifest_unknown_param_type"
MANIFEST_UNKNOWN_SECRET_SCOPE = "manifest_unknown_secret_scope"
MANIFEST_PARSE_ERROR = "manifest_parse_error"

# Repo structure
REPO_MISSING_README = "repo_missing_readme"
REPO_MISSING_LICENSE = "repo_missing_license"
REPO_NO_COMPONENTS_FOUND = "repo_no_components_found"
REPO_COMPONENT_FILE_MISSING = "repo_component_file_missing"
REPO_UNKNOWN_COMPONENT_TYPE = "repo_unknown_component_type"

# Routines (JSON components)
ROUTINE_MISSING_STEPS = "routine_missing_steps"
ROUTINE_MISSING_TRIGGER_PHRASES = "routine_missing_trigger_phrases"
ROUTINE_MISSING_RESPONSE_INSTRUCTION = "routine_missing_response_instruction"
ROUTINE_STEP_MISSING_COMMAND = "routine_step_missing_command"
ROUTINE_INVALID_JSON = "routine_invalid_json"

# Submitted-pypi / apt (lands with #16 — pre-registered here)
APT_PACKAGE_NOT_ON_ALLOWLIST = "apt_package_not_on_allowlist"

# Post-install ops (#19)
POST_INSTALL_OP_UNKNOWN_TYPE = "post_install_op_unknown_type"
POST_INSTALL_OP_MISSING_TARGET = "post_install_op_missing_target"
POST_INSTALL_OP_NOT_ON_ALLOWLIST = "post_install_op_not_on_allowlist"

# Lockfile resolution (#21)
LOCKFILE_RESOLUTION_FAILED = "lockfile_resolution_failed"
RESOLVED_LOCKFILE_EXCEEDS_SIZE_CAP = "resolved_lockfile_exceeds_size_cap"

# Synthetic — used by the dual-shape reader to wrap pre-#18 stored rows.
# Frontend clients render these as plain message strings.
LEGACY_UNSTRUCTURED = "legacy_unstructured"


ALL_REASON_CODES: frozenset[str] = frozenset({
    STATIC_ANALYSIS_DISALLOWED_PRIMITIVE,
    STATIC_ANALYSIS_RAW_DB_IMPORT,
    STATIC_ANALYSIS_SQL_MUTATION,
    STATIC_ANALYSIS_CROSS_COMMAND_ACCESS,
    STATIC_ANALYSIS_SHADOWS_BUILTIN_DIR,
    STATIC_ANALYSIS_SYNTAX_ERROR,
    STATIC_ANALYSIS_MISSING_BASE_CLASS,
    STATIC_ANALYSIS_TRANSITIVE_INHERITANCE,
    MANIFEST_BAD_SEMVER,
    MANIFEST_MISSING_REQUIRED_FIELD,
    MANIFEST_INVALID_FIELD_TYPE,
    MANIFEST_UNKNOWN_CATEGORY,
    MANIFEST_UNKNOWN_PARAM_TYPE,
    MANIFEST_UNKNOWN_SECRET_SCOPE,
    MANIFEST_PARSE_ERROR,
    REPO_MISSING_README,
    REPO_MISSING_LICENSE,
    REPO_NO_COMPONENTS_FOUND,
    REPO_COMPONENT_FILE_MISSING,
    REPO_UNKNOWN_COMPONENT_TYPE,
    ROUTINE_MISSING_STEPS,
    ROUTINE_MISSING_TRIGGER_PHRASES,
    ROUTINE_MISSING_RESPONSE_INSTRUCTION,
    ROUTINE_STEP_MISSING_COMMAND,
    ROUTINE_INVALID_JSON,
    APT_PACKAGE_NOT_ON_ALLOWLIST,
    POST_INSTALL_OP_UNKNOWN_TYPE,
    POST_INSTALL_OP_MISSING_TARGET,
    POST_INSTALL_OP_NOT_ON_ALLOWLIST,
    LOCKFILE_RESOLUTION_FAILED,
    RESOLVED_LOCKFILE_EXCEEDS_SIZE_CAP,
    LEGACY_UNSTRUCTURED,
})


_DOCS_BASE = "https://docs.jarvisautomation.dev/pantry/rejections"


def doc_url(reason_code: str) -> str:
    """Doc URL for a given reason_code.

    Unknown codes still return a well-formed URL — the docs site decides what
    to render for an unknown anchor. Defensive so the envelope never breaks
    on a stale or typo'd code.
    """
    return f"{_DOCS_BASE}#{reason_code}"


@dataclass
class Finding:
    """Single structured finding emitted during validation.

    Required fields: ``reason_code``, ``severity``, ``doc_url``.
    Optional fields fall into three groups:
    - Location: ``file`` / ``line`` / ``snippet`` (set for source-level findings)
    - Rule-specific: ``primitive`` (eval/exec/etc.), ``value`` (the offending value)
    - Human-readable: ``message`` (fallback prose for unstructured/legacy items)
    """

    reason_code: str
    severity: Literal["error", "warning"]
    doc_url: str
    file: str | None = None
    line: int | None = None
    snippet: str | None = None
    primitive: str | None = None
    value: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the wire envelope. Omits None fields for a clean payload."""
        d: dict[str, Any] = {
            "reason_code": self.reason_code,
            "severity": self.severity,
            "doc_url": self.doc_url,
        }
        for key in ("file", "line", "snippet", "primitive", "value", "message"):
            v = getattr(self, key)
            if v is not None:
                d[key] = v
        return d


def make_finding(
    reason_code: str,
    severity: Literal["error", "warning"],
    *,
    file: str | None = None,
    line: int | None = None,
    snippet: str | None = None,
    primitive: str | None = None,
    value: str | None = None,
    message: str | None = None,
) -> Finding:
    """Convenience constructor — auto-populates ``doc_url`` from the catalogue."""
    return Finding(
        reason_code=reason_code,
        severity=severity,
        doc_url=doc_url(reason_code),
        file=file,
        line=line,
        snippet=snippet,
        primitive=primitive,
        value=value,
        message=message,
    )
