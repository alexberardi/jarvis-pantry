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

# Map component type → (base class name, required methods)
COMPONENT_TYPE_INFO: dict[str, tuple[str, list[str]]] = {
    "command": ("IJarvisCommand", REQUIRED_COMMAND_METHODS),
    "agent": ("IJarvisAgent", REQUIRED_AGENT_METHODS),
    "device_protocol": ("IJarvisDeviceProtocol", REQUIRED_PROTOCOL_METHODS),
    "device_manager": ("IJarvisDeviceManager", REQUIRED_DEVICE_MANAGER_METHODS),
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
    """Result of static analysis on a command."""

    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dangerous_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "warnings": self.warnings,
            "errors": self.errors,
            "dangerous_patterns": self.dangerous_patterns,
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
                result.errors.append(f"Failed to parse manifest: {e}")
                result.passed = False
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
            result.passed = False
            result.errors.append("No components found in repo (no components in manifest and no recognized directory structure)")
            return result

    # 3. Analyze each component
    for comp in components:
        comp_type = comp.get("type", "command")
        comp_name = comp.get("name", "?")
        comp_path = comp.get("path", "command.py")

        source_path = repo_dir / comp_path
        if not source_path.exists():
            result.passed = False
            result.errors.append(f"Component '{comp_name}': {comp_path} not found")
            continue

        source = source_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=comp_path)
        except SyntaxError as e:
            result.passed = False
            result.errors.append(f"SyntaxError in {comp_path}: {e}")
            continue

        # Find the expected base class and check methods
        type_info = COMPONENT_TYPE_INFO.get(comp_type)
        if not type_info:
            result.warnings.append(f"Component '{comp_name}': unknown type '{comp_type}'")
            continue

        base_class_name, required_methods = type_info
        target_class = _find_class_by_base(tree, base_class_name)

        if target_class is None:
            result.passed = False
            result.errors.append(
                f"Component '{comp_name}': no class inheriting from {base_class_name} found in {comp_path}"
            )
            continue

        defined_names = _get_class_defined_names(target_class)
        for method in required_methods:
            if method not in defined_names:
                result.errors.append(f"Component '{comp_name}': missing required method/property: {method}")
                result.passed = False

        # Dangerous pattern detection (shared across all types)
        manifest_cmd_name = manifest.get("name") if manifest else None
        dangerous = _find_dangerous_patterns(tree, command_name=manifest_cmd_name)
        result.dangerous_patterns.extend(dangerous)

    if result.dangerous_patterns:
        result.warnings.append(f"Found {len(result.dangerous_patterns)} potentially dangerous pattern(s)")

    # 4. Check for shared dirs that shadow node built-in packages (bundles)
    if components:
        _check_shared_dir_conflicts(repo_dir, components, result)

    # 5. Deep manifest validation
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
            result.warnings.append(
                f"Shared directory '{entry.name}/' shadows a node built-in package. "
                f"Rename to something package-specific (e.g., 'shared/' or 'lib/')."
            )


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
    return names


def _find_dangerous_patterns(tree: ast.Module, command_name: str | None = None) -> list[str]:
    """Walk AST and flag dangerous patterns."""
    patterns: list[str] = []
    uses_data_repo = False

    for node in ast.walk(tree):
        # Direct calls: eval(), exec(), compile(), __import__()
        if isinstance(node, ast.Call):
            call_name = _get_name(node.func)
            if call_name and call_name in DANGEROUS_CALLS:
                patterns.append(f"Dangerous call: {call_name}()")

        # Imports
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DANGEROUS_MODULES:
                    patterns.append(f"Dangerous import: {alias.name}")
                elif root in DATABASE_MODULES:
                    patterns.append(f"Raw database import: {alias.name} (use CommandDataRepository instead)")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in DANGEROUS_MODULES:
                    patterns.append(f"Dangerous import: from {node.module}")
                elif root in DATABASE_MODULES:
                    patterns.append(f"Raw database import: from {node.module} (use CommandDataRepository instead)")
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
                    break  # one hit per string is enough

    # Check for cross-command data access: calls to repo methods with a
    # command_name string that doesn't match the command's own name
    if uses_data_repo and command_name:
        cross_access = _check_cross_command_access(tree, command_name)
        patterns.extend(cross_access)

    return patterns


def _check_cross_command_access(tree: ast.Module, command_name: str) -> list[str]:
    """Detect CommandDataRepository calls that use a different command's name."""
    patterns: list[str] = []
    repo_methods = {"save", "get", "get_all", "delete", "delete_all"}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Look for repo.save("other_command", ...) or repo.get("other_command", ...)
        if isinstance(node.func, ast.Attribute) and node.func.attr in repo_methods:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                accessed_name = node.args[0].value
                if accessed_name != command_name:
                    patterns.append(
                        f"Cross-command data access: accesses '{accessed_name}' data "
                        f"(this command is '{command_name}')"
                    )

    return patterns


def _validate_manifest_deep(manifest: dict[str, Any], result: StaticAnalysisResult) -> None:
    """Deep validation of manifest fields."""
    # Version must be semver
    version = manifest.get("version", "")
    if version and not SEMVER_RE.match(str(version)):
        result.errors.append(f"Invalid semver version: {version}")
        result.passed = False

    # Categories must be valid
    categories = manifest.get("categories", [])
    if isinstance(categories, list):
        for cat in categories:
            if cat not in VALID_CATEGORIES:
                result.warnings.append(f"Unknown category: {cat}")

    # Parameters validation
    parameters = manifest.get("parameters", [])
    if isinstance(parameters, list):
        for param in parameters:
            if isinstance(param, dict):
                pt = param.get("param_type")
                if pt and pt not in VALID_PARAM_TYPES:
                    result.warnings.append(f"Unknown param_type: {pt} for parameter {param.get('name', '?')}")

    # Secrets validation
    secrets = manifest.get("secrets", [])
    if isinstance(secrets, list):
        for secret in secrets:
            if isinstance(secret, dict):
                scope = secret.get("scope")
                if scope and scope not in VALID_SECRET_SCOPES:
                    result.warnings.append(f"Unknown secret scope: {scope} for key {secret.get('key', '?')}")
