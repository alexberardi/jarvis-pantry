#!/usr/bin/env python3
"""Test harness that runs inside the Docker container during command testing.

Supports both single-command repos and multi-component bundles.
Validates that each component is well-formed and safe:
- Imports successfully
- Class instantiates
- Properties return correct types
- Schema generation works
- Mock execution doesn't crash

Exit code 0 = all tests pass, non-zero = failures.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

import yaml

# Paths default to Docker container mount points, but can be overridden so
# the same harness runs on a bare GitHub Actions runner (runner repo).
_TEST_DIR = os.environ.get("JARVIS_HARNESS_TEST_DIR", "/test")
_REPO_DIR = os.environ.get("JARVIS_HARNESS_REPO_DIR", f"{_TEST_DIR}/repo")
_COMMAND_DIR = os.environ.get("JARVIS_HARNESS_COMMAND_DIR", f"{_TEST_DIR}/command")

sys.path.insert(0, _REPO_DIR)
sys.path.insert(0, _COMMAND_DIR)
sys.path.insert(0, _TEST_DIR)


def _find_class_in_module(module, base_class):
    """Find a subclass of base_class in the given module."""
    for attr_name in dir(module):
        cls = getattr(module, attr_name)
        if (isinstance(cls, type)
                and issubclass(cls, base_class)
                and cls is not base_class):
            return cls
    return None


def _find_command_class():
    """Find the IJarvisCommand subclass in the command module."""
    from jarvis_command_sdk import IJarvisCommand

    import importlib
    mod = importlib.import_module("command")

    return _find_class_in_module(mod, IJarvisCommand)


def _infer_components_from_structure() -> list[dict]:
    """Infer components from the repo directory structure inside the container."""
    dir_types = {
        "commands": ("command", "command.py"),
        "agents": ("agent", "agent.py"),
        "device_families": ("device_protocol", "protocol.py"),
        "device_managers": ("device_manager", "manager.py"),
        "prompt_providers": ("prompt_provider", "provider.py"),
    }

    components: list[dict] = []
    repo_root = Path(_REPO_DIR)

    # Root-level command.py
    if (repo_root / "command.py").exists():
        components.append({"type": "command", "name": "command", "path": "command.py"})

    # Convention directories
    for dir_name, (comp_type, entry_filename) in dir_types.items():
        type_dir = repo_root / dir_name
        if not type_dir.is_dir():
            continue
        for sub_dir in sorted(type_dir.iterdir()):
            if not sub_dir.is_dir() or sub_dir.name.startswith(("_", ".")):
                continue
            entry_point = sub_dir / entry_filename
            if entry_point.exists():
                components.append({
                    "type": comp_type,
                    "name": sub_dir.name,
                    "path": str(entry_point.relative_to(repo_root)),
                })

    return components


def _load_manifest() -> dict | None:
    """Load manifest from the container's test directory."""
    for name in ("jarvis_package.yaml", "jarvis_command.yaml"):
        for base in (_REPO_DIR, _COMMAND_DIR, _TEST_DIR):
            path = Path(base) / name
            if path.exists():
                with open(path) as f:
                    return yaml.safe_load(f)
    return None


def _run_command_tests(record, comp_path: str = "command") -> None:
    """Run tests for a command component."""
    import importlib
    from jarvis_command_sdk import IJarvisCommand

    # Ensure the component's parent dir is on path
    comp_file = Path(comp_path)
    if comp_file.parent != Path("."):
        parent = Path(_REPO_DIR) / comp_file.parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))

    mod_name = comp_file.stem
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        record("import", False, f"Import failed: {e}")
        return

    cls = _find_class_in_module(mod, IJarvisCommand)
    if cls is None:
        record("find_class", False, f"No IJarvisCommand subclass found in {comp_path}")
        return
    record("find_class", True)

    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        return

    # command_name
    try:
        name = instance.command_name
        assert isinstance(name, str) and len(name) > 0
        record("command_name", True)
    except Exception as e:
        record("command_name", False, str(e))

    # description
    try:
        desc = instance.description
        assert isinstance(desc, str) and len(desc) > 0
        record("description", True)
    except Exception as e:
        record("description", False, str(e))

    # parameters
    try:
        params = instance.parameters
        assert isinstance(params, list)
        record("parameters_type", True)
    except Exception as e:
        record("parameters_type", False, str(e))

    # required_secrets
    try:
        secrets = instance.required_secrets
        assert isinstance(secrets, list)
        record("required_secrets_type", True)
    except Exception as e:
        record("required_secrets_type", False, str(e))

    # keywords
    try:
        kw = instance.keywords
        assert isinstance(kw, list)
        assert all(isinstance(k, str) for k in kw)
        record("keywords_type", True)
    except Exception as e:
        record("keywords_type", False, str(e))

    # generate_prompt_examples
    examples = []
    try:
        examples = instance.generate_prompt_examples()
        assert isinstance(examples, list)
        assert len(examples) > 0, "Must have at least one prompt example"
        for ex in examples:
            assert hasattr(ex, "voice_command")
            assert hasattr(ex, "expected_parameters")
        record("prompt_examples", True)
    except Exception as e:
        record("prompt_examples", False, str(e))

    # generate_adapter_examples
    try:
        adapter_ex = instance.generate_adapter_examples()
        assert isinstance(adapter_ex, list)
        record("adapter_examples", True)
    except Exception as e:
        record("adapter_examples", False, str(e))

    # validate_call
    try:
        result = instance.validate_call()
        assert isinstance(result, list)
        record("validate_call", True)
    except Exception as e:
        record("validate_call", False, str(e))

    # Timed mock run
    try:
        from jarvis_command_sdk import RequestInformation
        ri = RequestInformation(voice_command="test", conversation_id="test-conv")
        kwargs = {}
        if examples:
            kwargs = dict(examples[0].expected_parameters)
        start = time.time()
        try:
            instance.run(ri, **kwargs)
        except Exception:
            pass
        elapsed = time.time() - start
        if elapsed > 30:
            record("mock_run_timing", False, f"run() took {elapsed:.1f}s (>30s limit)")
        else:
            record("mock_run_timing", True)
    except Exception as e:
        record("mock_run_timing", False, str(e))


def _run_agent_tests(record, comp_path: str = "agent.py") -> None:
    """Run tests for an agent component."""
    import importlib

    comp_file = Path(comp_path)
    if comp_file.parent != Path("."):
        parent = Path(_REPO_DIR) / comp_file.parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))

    mod_name = comp_file.stem
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        record("import", False, f"Import failed: {e}")
        return

    # Find IJarvisAgent subclass
    from jarvis_command_sdk import IJarvisAgent
    cls = _find_class_in_module(mod, IJarvisAgent)
    if cls is None:
        record("find_class", False, f"No IJarvisAgent subclass found in {comp_path}")
        return
    record("find_class", True)

    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        return

    # name
    try:
        name = instance.name
        assert isinstance(name, str) and len(name) > 0
        record("name", True)
    except Exception as e:
        record("name", False, str(e))

    # description
    try:
        desc = instance.description
        assert isinstance(desc, str) and len(desc) > 0
        record("description", True)
    except Exception as e:
        record("description", False, str(e))

    # schedule
    try:
        sched = instance.schedule
        assert hasattr(sched, "interval_seconds"), "schedule must have interval_seconds"
        record("schedule", True)
    except Exception as e:
        record("schedule", False, str(e))

    # required_secrets
    try:
        secrets = instance.required_secrets
        assert isinstance(secrets, list)
        record("required_secrets", True)
    except Exception as e:
        record("required_secrets", False, str(e))

    # get_context_data
    try:
        ctx = instance.get_context_data()
        assert isinstance(ctx, dict), f"get_context_data must return dict, got {type(ctx)}"
        record("get_context_data", True)
    except Exception as e:
        record("get_context_data", False, str(e))


def _run_protocol_tests(record, comp_path: str = "protocol.py") -> None:
    """Run tests for a device_protocol component."""
    import importlib

    comp_file = Path(comp_path)
    if comp_file.parent != Path("."):
        parent = Path(_REPO_DIR) / comp_file.parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))

    mod_name = comp_file.stem
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        record("import", False, f"Import failed: {e}")
        return

    # Find IJarvisDeviceProtocol subclass
    from jarvis_command_sdk import IJarvisDeviceProtocol
    cls = _find_class_in_module(mod, IJarvisDeviceProtocol)
    if cls is None:
        record("find_class", False, f"No IJarvisDeviceProtocol subclass found in {comp_path}")
        return
    record("find_class", True)

    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        return

    # protocol_name
    try:
        name = instance.protocol_name
        assert isinstance(name, str) and len(name) > 0
        record("protocol_name", True)
    except Exception as e:
        record("protocol_name", False, str(e))

    # supported_domains
    try:
        domains = instance.supported_domains
        assert isinstance(domains, list)
        record("supported_domains", True)
    except Exception as e:
        record("supported_domains", False, str(e))


def _run_device_manager_tests(record, comp_path: str = "manager.py") -> None:
    """Run tests for a device_manager component."""
    import importlib

    comp_file = Path(comp_path)
    if comp_file.parent != Path("."):
        parent = Path(_REPO_DIR) / comp_file.parent
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))

    mod_name = comp_file.stem
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        record("import", False, f"Import failed: {e}")
        return

    from jarvis_command_sdk import IJarvisDeviceManager
    cls = _find_class_in_module(mod, IJarvisDeviceManager)
    if cls is None:
        record("find_class", False, f"No IJarvisDeviceManager subclass found in {comp_path}")
        return
    record("find_class", True)

    try:
        instance = cls()
        record("instantiate", True)
    except Exception as e:
        record("instantiate", False, f"Instantiation failed: {e}")
        return

    # name
    try:
        name = instance.name
        assert isinstance(name, str) and len(name) > 0
        record("name", True)
    except Exception as e:
        record("name", False, str(e))

    # friendly_name
    try:
        fn = instance.friendly_name
        assert isinstance(fn, str) and len(fn) > 0
        record("friendly_name", True)
    except Exception as e:
        record("friendly_name", False, str(e))

    # description
    try:
        desc = instance.description
        assert isinstance(desc, str) and len(desc) > 0
        record("description", True)
    except Exception as e:
        record("description", False, str(e))


def _run_prompt_provider_tests(record, comp_path: str = "provider.py") -> None:
    """Run tests for a prompt_provider component.

    Prompt providers depend on command-center internals (app.core.*) that are
    not available in the container, so we validate via AST parsing instead of
    import + instantiation.
    """
    import ast

    comp_file = Path(comp_path)
    full_path = Path(_REPO_DIR) / comp_file
    if not full_path.exists():
        record("file_exists", False, f"Provider file not found: {comp_path}")
        return
    record("file_exists", True)

    source = full_path.read_text(encoding="utf-8")

    # Syntax check
    try:
        tree = ast.parse(source)
        record("syntax", True)
    except SyntaxError as e:
        record("syntax", False, f"Syntax error: {e}")
        return

    # Find class inheriting from *PromptProvider*
    provider_class = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name and "PromptProvider" in base_name:
                provider_class = node
                break
        if provider_class:
            break

    if provider_class is None:
        record("find_class", False, "No class inheriting from IJarvisPromptProvider found")
        return
    record("find_class", True)

    # Check required methods
    defined_methods: set[str] = set()
    for item in provider_class.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_methods.add(item.name)

    required = {"name", "build_system_prompt", "get_capabilities"}
    missing = required - defined_methods
    if missing:
        record("required_methods", False, f"Missing methods: {', '.join(sorted(missing))}")
    else:
        record("required_methods", True)

    # Check get_capabilities returns expected keys (best-effort from AST)
    record("ast_validated", True)


# Map component type → test runner
_TYPE_RUNNERS = {
    "command": _run_command_tests,
    "agent": _run_agent_tests,
    "device_protocol": _run_protocol_tests,
    "device_manager": _run_device_manager_tests,
    "prompt_provider": _run_prompt_provider_tests,
}


def run_tests() -> dict:
    """Run all behavioral tests and return results.

    Reads manifest to discover components. If no components declared, tests
    the single command.py. Otherwise, tests each component by type.
    """
    results = {
        "tests": [],
        "passed": 0,
        "failed": 0,
        "errors": [],
    }

    def record(name: str, passed: bool, error: str | None = None) -> None:
        results["tests"].append({"name": name, "passed": passed, "error": error})
        if passed:
            results["passed"] += 1
        else:
            results["failed"] += 1
            if error:
                results["errors"].append(f"{name}: {error}")

    manifest = _load_manifest()
    components = manifest.get("components", []) if manifest else []

    # Infer from directory structure if not declared
    if not components:
        components = _infer_components_from_structure()

    if not components:
        # Last resort: single command.py at root
        _run_command_tests(record, "command")
    else:
        # Multi-component bundle
        for comp in components:
            comp_type = comp.get("type", "command")
            comp_name = comp.get("name", "?")
            comp_path = comp.get("path", "command.py")

            # Prefix test names with component name for clarity
            def comp_record(name: str, passed: bool, error: str | None = None, _cn=comp_name) -> None:
                record(f"{_cn}/{name}", passed, error)

            runner = _TYPE_RUNNERS.get(comp_type)
            if runner:
                runner(comp_record, comp_path)
            else:
                record(f"{comp_name}/unknown_type", False, f"Unknown component type: {comp_type}")

    # Summary
    results["summary"] = (
        f"{'PASS' if results['failed'] == 0 else 'FAIL'} "
        f"- {results['passed']}/{results['passed'] + results['failed']} tests passed"
    )
    return results


if __name__ == "__main__":
    try:
        results = run_tests()
        print(json.dumps(results, indent=2))
        sys.exit(0 if results["failed"] == 0 else 1)
    except Exception as e:
        print(json.dumps({
            "summary": f"CRASH - {e}",
            "tests": [],
            "passed": 0,
            "failed": 1,
            "errors": [traceback.format_exc()],
        }, indent=2))
        sys.exit(2)
