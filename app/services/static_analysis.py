"""Static AST analysis for command submissions.

Performs fast, synchronous validation of command.py and manifest:
- AST parsing + class structure verification
- Dangerous pattern detection
- Deep manifest validation (semver, categories, param types, secret scopes)
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import rejection_codes as rc
from .rejection_codes import Finding, make_finding


# Duplicated from jarvis-node-setup/core/command_manifest.py (stable)
VALID_CATEGORIES: list[str] = [
    "automation", "calendar", "communication", "entertainment", "finance",
    "fitness", "food", "games", "health", "home", "information", "media",
    "music", "news", "productivity", "shopping", "smart-home", "sports",
    "travel", "utilities", "weather",
]

VALID_PARAM_TYPES: set[str] = {
    "string", "int", "float", "bool", "enum", "date", "time", "datetime",
}

VALID_SECRET_SCOPES: set[str] = {"integration", "node"}

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

REQUIRED_COMMAND_METHODS: list[str] = [
    "command_name", "description", "parameters", "required_secrets",
    "keywords", "run", "generate_prompt_examples", "generate_adapter_examples",
]

# Backward compat alias
REQUIRED_METHODS = REQUIRED_COMMAND_METHODS

REQUIRED_AGENT_METHODS: list[str] = [
    "name", "description", "schedule", "required_secrets", "run", "get_context_data",
]

REQUIRED_PROTOCOL_METHODS: list[str] = [
    "protocol_name", "supported_domains", "discover", "control", "get_state",
]

REQUIRED_DEVICE_MANAGER_METHODS: list[str] = [
    "name", "friendly_name", "description", "collect_devices",
]

REQUIRED_PROMPT_PROVIDER_METHODS: list[str] = [
    "name", "build_system_prompt", "get_capabilities",
]

# Map component type → (base class name, required methods)
COMPONENT_TYPE_INFO: dict[str, tuple[str, list[str]]] = {
    "command": ("IJarvisCommand", REQUIRED_COMMAND_METHODS),
    "agent": ("IJarvisAgent", REQUIRED_AGENT_METHODS),
    "device_protocol": ("IJarvisDeviceProtocol", REQUIRED_PROTOCOL_METHODS),
    "device_manager": ("IJarvisDeviceManager", REQUIRED_DEVICE_MANAGER_METHODS),
    "prompt_provider": ("IJarvisPromptProvider", REQUIRED_PROMPT_PROVIDER_METHODS),
}

DANGEROUS_MODULES: set[str] = {"subprocess", "os", "shutil", "ctypes", "importlib"}

DANGEROUS_CALLS: set[str] = {
    "eval", "exec", "compile", "__import__",
    "os.system", "os.popen", "os.exec", "os.execl", "os.execle",
    "os.execlp", "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawn", "os.spawnl", "os.spawnle",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_call", "subprocess.check_output",
}

# Calls whose presence is a hard rejection (not just a warning). Subset of
# DANGEROUS_CALLS — submissions invoking any of these are rejected outright.
# `compile` and `__import__` remain warn-only.
HARD_FAIL_CALLS: set[str] = {
    "eval", "exec",
    "os.system", "os.popen",
    "os.exec", "os.execl", "os.execle", "os.execlp",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "os.spawn", "os.spawnl", "os.spawnle",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_call", "subprocess.check_output",
}

# Database modules — flagged because commands should use CommandDataRepository
DATABASE_MODULES: set[str] = {"sqlite3", "sqlalchemy", "alembic", "psycopg2", "asyncpg", "aiosqlite", "peewee"}

# Node built-in top-level packages — community bundles must not shadow these.
# Shared code in bundles should use package-specific names (e.g. "lifx_shared")
# instead of generic names that collide with the node runtime.
NODE_BUILTIN_PACKAGES: set[str] = {
    "commands", "services", "utils", "core", "agents",
    "device_families", "device_managers", "provisioning",
    "repositories", "db", "vendor", "scripts",
}

# Allowed DB access patterns — these are the sanctioned data access layer imports
ALLOWED_DB_IMPORTS: set[str] = {"db", "repositories", "repositories.command_data_repository"}

# SQL keywords that indicate schema mutations (case-insensitive scan on string literals)
SQL_MUTATION_KEYWORDS: list[str] = [
    "CREATE TABLE", "ALTER TABLE", "DROP TABLE", "CREATE INDEX", "DROP INDEX",
    "INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE",
    "CREATE DATABASE", "DROP DATABASE", "GRANT ", "REVOKE ",
]


@dataclass
class StaticAnalysisResult:
    """Result of static analysis on a command.

    Carries the new structured ``findings`` list (#18) alongside the legacy
    flat-string lists kept for back-compat with stored DB rows + existing
    internal consumers. The HTTP rejection envelope emits only the new shape;
    ``to_dict()`` storage payload includes both for transparent dual-shape reads.
    """

    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dangerous_patterns: list[str] = field(default_factory=list)
    # New structured findings (#18). Populated in parallel with the legacy fields.
    findings: list[Finding] = field(default_factory=list)

    def add_finding(self, finding: Finding) -> None:
        """Append a structured finding. Caller still appends to legacy fields separately."""
        self.findings.append(finding)

    @property
    def reason_codes(self) -> list[str]:
        """Deduplicated list of reason codes across all findings (stable order)."""
        seen: list[str] = []
        for f in self.findings:
            if f.reason_code not in seen:
                seen.append(f.reason_code)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            # Legacy keys — kept so pre-#18 readers still work, and so the
            # status-endpoint dual-shape detector can see the old shape.
            "warnings": self.warnings,
            "errors": self.errors,
            "dangerous_patterns": self.dangerous_patterns,
            # New structured shape (#18). Findings are split by severity for
            # easy client consumption; reason_codes is a deduped index.
            "findings": [f.to_dict() for f in self.findings if f.severity == "error"],
            "warnings_structured": [f.to_dict() for f in self.findings if f.severity == "warning"],
            "reason_codes": self.reason_codes,
            "checks_passed": self._checks_passed,
        }

    @property
    def _checks_passed(self) -> int:
        """Approximate number of checks that passed (for display)."""
        total = 8  # syntax, class, inheritance, methods, manifest_schema, semver, categories, params
        return total - len(self.errors)


def run_static_analysis(repo_dir: Path) -> StaticAnalysisResult:
    """Run static analysis on a command repo.

    Supports both single-command repos and multi-component bundle repos.

    Args:
        repo_dir: Path to the cloned repo directory.

    Returns:
        StaticAnalysisResult with pass/fail, warnings, errors, and dangerous patterns.
    """
    result = StaticAnalysisResult(passed=True)

    # 1. Parse manifest
    manifest: dict[str, Any] | None = None
    for manifest_name in ("jarvis_package.yaml", "jarvis_command.yaml"):
        manifest_path = repo_dir / manifest_name
        if manifest_path.exists():
            import yaml
            try:
                with open(manifest_path) as f:
                    manifest = yaml.safe_load(f)
                if not isinstance(manifest, dict):
                    manifest = None
            except Exception as e:
                msg = f"Failed to parse manifest: {e}"
                result.errors.append(msg)
                result.passed = False
                result.add_finding(make_finding(
                    rc.MANIFEST_PARSE_ERROR, "error",
                    file=manifest_name, message=msg,
                ))
            break

    # 2. Build component list (explicit or inferred from repo structure)
    components = []
    if manifest and manifest.get("components"):
        components = manifest["components"]
    else:
        # Infer from directory structure
        from .github_service import _infer_components_from_structure
        manifest_name = manifest.get("name", "unknown") if manifest else "unknown"
        components = _infer_components_from_structure(repo_dir, manifest_name)
        if not components:
            msg = "No components found in repo (no components in manifest and no recognized directory structure)"
            result.passed = False
            result.errors.append(msg)
            result.add_finding(make_finding(rc.REPO_NO_COMPONENTS_FOUND, "error", message=msg))
            return result

    # 3. Analyze each component
    for comp in components:
        comp_type = comp.get("type", "command")
        comp_name = comp.get("name", "?")
        comp_path = comp.get("path", "command.py")

        source_path = repo_dir / comp_path
        if not source_path.exists():
            msg = f"Component '{comp_name}': {comp_path} not found"
            result.passed = False
            result.errors.append(msg)
            result.add_finding(make_finding(
                rc.REPO_COMPONENT_FILE_MISSING, "error",
                file=comp_path, message=msg,
            ))
            continue

        # Routine components are JSON — validate structure, skip Python analysis
        if comp_type == "routine":
            import json
            try:
                with open(source_path) as f:
                    routine_data = json.load(f)
                if not routine_data.get("trigger_phrases"):
                    msg = f"Routine '{comp_name}': missing trigger_phrases"
                    result.warnings.append(msg)
                    result.add_finding(make_finding(
                        rc.ROUTINE_MISSING_TRIGGER_PHRASES, "warning",
                        file=comp_path, message=msg,
                    ))
                if not routine_data.get("steps"):
                    msg = f"Routine '{comp_name}': missing steps"
                    result.errors.append(msg)
                    result.passed = False
                    result.add_finding(make_finding(
                        rc.ROUTINE_MISSING_STEPS, "error",
                        file=comp_path, message=msg,
                    ))
                if not routine_data.get("response_instruction"):
                    msg = f"Routine '{comp_name}': missing response_instruction"
                    result.warnings.append(msg)
                    result.add_finding(make_finding(
                        rc.ROUTINE_MISSING_RESPONSE_INSTRUCTION, "warning",
                        file=comp_path, message=msg,
                    ))
                for i, step in enumerate(routine_data.get("steps", [])):
                    if not step.get("command"):
                        msg = f"Routine '{comp_name}': step {i+1} missing 'command'"
                        result.errors.append(msg)
                        result.passed = False
                        result.add_finding(make_finding(
                            rc.ROUTINE_STEP_MISSING_COMMAND, "error",
                            file=comp_path, value=str(i + 1), message=msg,
                        ))
            except json.JSONDecodeError as e:
                msg = f"Routine '{comp_name}': invalid JSON: {e}"
                result.passed = False
                result.errors.append(msg)
                result.add_finding(make_finding(
                    rc.ROUTINE_INVALID_JSON, "error",
                    file=comp_path, message=msg,
                ))
            except Exception as e:
                msg = f"Routine '{comp_name}': failed to read: {e}"
                result.passed = False
                result.errors.append(msg)
                result.add_finding(make_finding(
                    rc.ROUTINE_INVALID_JSON, "error",
                    file=comp_path, message=msg,
                ))
            continue

        source = source_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=comp_path)
        except SyntaxError as e:
            msg = f"SyntaxError in {comp_path}: {e}"
            result.passed = False
            result.errors.append(msg)
            result.add_finding(make_finding(
                rc.STATIC_ANALYSIS_SYNTAX_ERROR, "error",
                file=comp_path, line=getattr(e, "lineno", None),
                message=msg,
            ))
            continue

        # Find the expected base class and check methods
        type_info = COMPONENT_TYPE_INFO.get(comp_type)
        if not type_info:
            msg = f"Component '{comp_name}': unknown type '{comp_type}'"
            result.warnings.append(msg)
            result.add_finding(make_finding(
                rc.REPO_UNKNOWN_COMPONENT_TYPE, "warning",
                file=comp_path, value=comp_type, message=msg,
            ))
            continue

        base_class_name, required_methods = type_info
        target_class = _find_class_by_base(tree, base_class_name)

        if target_class is None:
            jarvis_deps = manifest.get("jarvis_dependencies", []) if manifest else []
            if jarvis_deps:
                # Package declares Jarvis dependencies — the class may inherit
                # from a dependency's class (transitive SDK base). Accept this
                # and let the container test validate the inheritance chain.
                any_class = _find_any_class_with_bases(tree)
                if any_class is None:
                    msg = f"Component '{comp_name}': no class definition found in {comp_path}"
                    result.passed = False
                    result.errors.append(msg)
                    result.add_finding(make_finding(
                        rc.STATIC_ANALYSIS_MISSING_BASE_CLASS, "error",
                        file=comp_path, value=base_class_name, message=msg,
                    ))
                    continue
                msg = (
                    f"Component '{comp_name}': no direct {base_class_name} base class found. "
                    f"Declared jarvis_dependencies={jarvis_deps} — transitive inheritance "
                    f"will be validated in container tests."
                )
                result.warnings.append(msg)
                result.add_finding(make_finding(
                    rc.STATIC_ANALYSIS_TRANSITIVE_INHERITANCE, "warning",
                    file=comp_path, value=base_class_name, message=msg,
                ))
                target_class = any_class
            else:
                msg = f"Component '{comp_name}': no class inheriting from {base_class_name} found in {comp_path}"
                result.passed = False
                result.errors.append(msg)
                result.add_finding(make_finding(
                    rc.STATIC_ANALYSIS_MISSING_BASE_CLASS, "error",
                    file=comp_path, value=base_class_name, message=msg,
                ))
                continue

        defined_names = _get_class_defined_names(target_class)
        for method in required_methods:
            if method not in defined_names:
                msg = f"Component '{comp_name}': missing required method/property: {method}"
                result.errors.append(msg)
                result.passed = False
                result.add_finding(make_finding(
                    rc.MANIFEST_MISSING_REQUIRED_FIELD, "error",
                    file=comp_path, value=method, message=msg,
                ))

        # Dangerous pattern detection (shared across all types)
        manifest_cmd_name = manifest.get("name") if manifest else None
        dangerous, structured = _find_dangerous_patterns(
            tree, source=source, command_name=manifest_cmd_name, file=comp_path,
        )
        result.dangerous_patterns.extend(dangerous)
        for f_ in structured:
            result.add_finding(f_)

    if result.dangerous_patterns:
        result.warnings.append(f"Found {len(result.dangerous_patterns)} potentially dangerous pattern(s)")

    # 4. Check for shared dirs that shadow node built-in packages (bundles)
    if components:
        _check_shared_dir_conflicts(repo_dir, components, result)

    # 5. Check for README.md and LICENSE
    readme_found = any(
        (repo_dir / name).exists()
        for name in ("README.md", "readme.md", "Readme.md", "README.MD")
    )
    if not readme_found:
        msg = "Missing README.md — a README is required for Pantry submission"
        result.warnings.append(msg)
        result.add_finding(make_finding(rc.REPO_MISSING_README, "warning", message=msg))

    license_found = any(
        (repo_dir / name).exists()
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "license", "LICENCE")
    )
    if not license_found:
        msg = "Missing LICENSE — a license file is required for Pantry submission"
        result.warnings.append(msg)
        result.add_finding(make_finding(rc.REPO_MISSING_LICENSE, "warning", message=msg))

    # 6. Deep manifest validation
    if manifest:
        _validate_manifest_deep(manifest, result)

    return result


def _check_shared_dir_conflicts(
    repo_dir: Path, components: list[dict[str, Any]], result: StaticAnalysisResult
) -> None:
    """Check if the repo has shared directories that shadow node built-in packages.

    Community bundles install shared code to a lib dir that's added to sys.path.
    If that lib dir contains packages named 'services', 'utils', 'core', etc.,
    they'll shadow the node's built-in packages and break things.
    """
    # Collect top-level dirs used by component entry points
    component_top_dirs: set[str] = set()
    for comp in components:
        parts = Path(comp.get("path", "")).parts
        if len(parts) > 1:
            component_top_dirs.add(parts[0])

    # Check remaining top-level dirs for conflicts
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
    for entry in repo_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in skip or entry.name.startswith("."):
            continue
        if entry.name in component_top_dirs:
            continue
        if entry.name in NODE_BUILTIN_PACKAGES and any(entry.rglob("*.py")):
            msg = (
                f"Shared directory '{entry.name}/' shadows a node built-in package. "
                f"Rename to something package-specific (e.g., 'shared/' or 'lib/')."
            )
            result.warnings.append(msg)
            result.add_finding(make_finding(
                rc.STATIC_ANALYSIS_SHADOWS_BUILTIN_DIR, "warning",
                value=entry.name, message=msg,
            ))


def _find_command_class(tree: ast.Module) -> ast.ClassDef | None:
    """Find a class that inherits from IJarvisCommand."""
    return _find_class_by_base(tree, "IJarvisCommand")


def _find_class_by_base(tree: ast.Module, base_class_name: str) -> ast.ClassDef | None:
    """Find a class that inherits from the given base class name."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = _get_name(base)
            if name and base_class_name in name:
                return node
    return None


def _find_any_class_with_bases(tree: ast.Module) -> ast.ClassDef | None:
    """Find the first class that inherits from something.

    Used when ``jarvis_dependencies`` is declared — the component may
    inherit from a dependency's class rather than directly from an SDK
    base. We still need to find *a* class to check required methods on.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.bases:
            return node
    return None


def _get_name(node: ast.expr) -> str | None:
    """Extract name from an AST node (Name or Attribute)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _get_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _get_class_defined_names(cls: ast.ClassDef) -> set[str]:
    """Get all method/property names defined in a class."""
    names: set[str] = set()
    for node in cls.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _snippet_for_line(source: str | None, line: int | None) -> str | None:
    """Return the source line at the given 1-based line number, if available."""
    if source is None or line is None or line < 1:
        return None
    lines = source.splitlines()
    if line > len(lines):
        return None
    return lines[line - 1].strip()


def _find_dangerous_patterns(
    tree: ast.Module,
    *,
    source: str | None = None,
    command_name: str | None = None,
    file: str | None = None,
) -> tuple[list[str], list[Finding]]:
    """Walk AST and flag dangerous patterns.

    Returns ``(legacy_strings, structured_findings)`` — the legacy list is kept
    for back-compat with the ``dangerous_patterns: list[str]`` field; the
    structured list carries reason_codes + file/line/snippet for the #18 envelope.
    """
    patterns: list[str] = []
    findings: list[Finding] = []
    uses_data_repo = False

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        snippet = _snippet_for_line(source, line)

        # Direct calls: eval(), exec(), compile(), __import__()
        if isinstance(node, ast.Call):
            call_name = _get_name(node.func)
            if call_name and call_name in DANGEROUS_CALLS:
                patterns.append(f"Dangerous call: {call_name}()")
                primitive = call_name.split(".")[0]
                findings.append(make_finding(
                    rc.STATIC_ANALYSIS_DISALLOWED_PRIMITIVE, "warning",
                    file=file, line=line, snippet=snippet, primitive=primitive,
                    value=call_name,
                ))

        # Imports
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DANGEROUS_MODULES:
                    patterns.append(f"Dangerous import: {alias.name}")
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_DISALLOWED_PRIMITIVE, "warning",
                        file=file, line=line, snippet=snippet, primitive=root,
                        value=alias.name,
                    ))
                elif root in DATABASE_MODULES:
                    patterns.append(f"Raw database import: {alias.name} (use CommandDataRepository instead)")
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_RAW_DB_IMPORT, "warning",
                        file=file, line=line, snippet=snippet, primitive=root,
                        value=alias.name,
                    ))

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in DANGEROUS_MODULES:
                    patterns.append(f"Dangerous import: from {node.module}")
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_DISALLOWED_PRIMITIVE, "warning",
                        file=file, line=line, snippet=snippet, primitive=root,
                        value=node.module,
                    ))
                elif root in DATABASE_MODULES:
                    patterns.append(f"Raw database import: from {node.module} (use CommandDataRepository instead)")
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_RAW_DB_IMPORT, "warning",
                        file=file, line=line, snippet=snippet, primitive=root,
                        value=node.module,
                    ))
                elif node.module in ALLOWED_DB_IMPORTS:
                    # Sanctioned data access — check what they're importing
                    imported_names = [a.name for a in (node.names or [])]
                    if "CommandDataRepository" in imported_names:
                        uses_data_repo = True

        # String literals containing SQL mutation keywords
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            for kw in SQL_MUTATION_KEYWORDS:
                if kw in upper:
                    patterns.append(f"SQL mutation detected: contains '{kw}'")
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_SQL_MUTATION, "warning",
                        file=file, line=line,
                        snippet=node.value if len(node.value) < 200 else node.value[:200],
                        value=kw.strip(),
                    ))
                    break  # one hit per string is enough

    # Check for cross-command data access: calls to repo methods with a
    # command_name string that doesn't match the command's own name
    if uses_data_repo and command_name:
        cross_access_legacy, cross_access_findings = _check_cross_command_access(
            tree, command_name, source=source, file=file,
        )
        patterns.extend(cross_access_legacy)
        findings.extend(cross_access_findings)

    return patterns, findings


def _check_cross_command_access(
    tree: ast.Module,
    command_name: str,
    *,
    source: str | None = None,
    file: str | None = None,
) -> tuple[list[str], list[Finding]]:
    """Detect CommandDataRepository calls that use a different command's name."""
    patterns: list[str] = []
    findings: list[Finding] = []
    repo_methods = {"save", "get", "get_all", "delete", "delete_all"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Look for repo.save("other_command", ...) or repo.get("other_command", ...)
        if isinstance(node.func, ast.Attribute) and node.func.attr in repo_methods:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                accessed_name = node.args[0].value
                if accessed_name != command_name:
                    line = getattr(node, "lineno", None)
                    patterns.append(
                        f"Cross-command data access: accesses '{accessed_name}' data "
                        f"(this command is '{command_name}')"
                    )
                    findings.append(make_finding(
                        rc.STATIC_ANALYSIS_CROSS_COMMAND_ACCESS, "warning",
                        file=file, line=line,
                        snippet=_snippet_for_line(source, line),
                        value=accessed_name,
                        message=(
                            f"Cross-command data access: accesses '{accessed_name}' "
                            f"data (this command is '{command_name}')"
                        ),
                    ))

    return patterns, findings


def _validate_manifest_deep(manifest: dict[str, Any], result: StaticAnalysisResult) -> None:
    """Deep validation of manifest fields."""
    # Version must be semver
    version = manifest.get("version", "")
    if version and not SEMVER_RE.match(str(version)):
        msg = f"Invalid semver version: {version}"
        result.errors.append(msg)
        result.passed = False
        result.add_finding(make_finding(
            rc.MANIFEST_BAD_SEMVER, "error",
            value=str(version), message=msg,
        ))

    # Categories must be valid
    categories = manifest.get("categories", [])
    if isinstance(categories, list):
        for cat in categories:
            if cat not in VALID_CATEGORIES:
                msg = f"Unknown category: {cat}"
                result.warnings.append(msg)
                result.add_finding(make_finding(
                    rc.MANIFEST_UNKNOWN_CATEGORY, "warning",
                    value=str(cat), message=msg,
                ))

    # Parameters validation
    parameters = manifest.get("parameters", [])
    if isinstance(parameters, list):
        for param in parameters:
            if isinstance(param, dict):
                pt = param.get("param_type")
                if pt and pt not in VALID_PARAM_TYPES:
                    msg = f"Unknown param_type: {pt} for parameter {param.get('name', '?')}"
                    result.warnings.append(msg)
                    result.add_finding(make_finding(
                        rc.MANIFEST_UNKNOWN_PARAM_TYPE, "warning",
                        value=str(pt), message=msg,
                    ))

    # Secrets validation
    secrets = manifest.get("secrets", [])
    if isinstance(secrets, list):
        for secret in secrets:
            if isinstance(secret, dict):
                scope = secret.get("scope")
                if scope and scope not in VALID_SECRET_SCOPES:
                    msg = f"Unknown secret scope: {scope} for key {secret.get('key', '?')}"
                    result.warnings.append(msg)
                    result.add_finding(make_finding(
                        rc.MANIFEST_UNKNOWN_SECRET_SCOPE, "warning",
                        value=str(scope), message=msg,
                    ))
