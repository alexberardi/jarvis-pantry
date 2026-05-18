"""Tests for static analysis service."""

from pathlib import Path

import yaml

from app.services.static_analysis import (
    StaticAnalysisResult,
    run_static_analysis,
    VALID_CATEGORIES,
    VALID_PARAM_TYPES,
    VALID_SECRET_SCOPES,
)


def _make_repo(tmp_path: Path, command_py: str, manifest: dict | None = None) -> Path:
    """Create a minimal command repo for testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "command.py").write_text(command_py)
    if manifest is None:
        manifest = {"name": "test_cmd", "description": "Test command", "version": "1.0.0"}
    (repo / "jarvis_command.yaml").write_text(yaml.dump(manifest))
    (repo / "README.md").write_text("# Test")
    (repo / "LICENSE").write_text("MIT")
    return repo


VALID_COMMAND = """\
from jarvis_command_sdk import IJarvisCommand

class TestCommand(IJarvisCommand):
    @property
    def command_name(self):
        return "test_cmd"

    @property
    def description(self):
        return "A test command"

    @property
    def parameters(self):
        return []

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["test"]

    def run(self, request_info, **kwargs):
        return {"status": "ok"}

    def generate_prompt_examples(self):
        return []

    def generate_adapter_examples(self):
        return []
"""


class TestASTValidation:
    def test_valid_command_passes(self, tmp_path):
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert len(result.errors) == 0

    def test_syntax_error(self, tmp_path):
        repo = _make_repo(tmp_path, "def broken(:\n  pass")
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("SyntaxError" in e for e in result.errors)

    def test_no_components_found(self, tmp_path):
        """Repo with manifest but no recognized source files fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "jarvis_command.yaml").write_text(yaml.dump({"name": "x", "description": "x", "version": "1.0.0"}))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("no components found" in e.lower() for e in result.errors)

    def test_no_ijarviscommand_subclass(self, tmp_path):
        repo = _make_repo(tmp_path, "class Foo:\n    pass\n")
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("IJarvisCommand" in e for e in result.errors)

    def test_missing_required_methods(self, tmp_path):
        # Class inherits IJarvisCommand but has no methods
        code = """\
from jarvis_command_sdk import IJarvisCommand

class Incomplete(IJarvisCommand):
    pass
"""
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("missing required" in e.lower() for e in result.errors)

    def test_partial_methods(self, tmp_path):
        # Only has command_name and description
        code = """\
from jarvis_command_sdk import IJarvisCommand

class Partial(IJarvisCommand):
    @property
    def command_name(self):
        return "partial"

    @property
    def description(self):
        return "partial"
"""
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        # Should be missing: parameters, required_secrets, keywords, run, generate_prompt_examples, generate_adapter_examples
        missing_count = sum(1 for e in result.errors if "missing required" in e.lower())
        assert missing_count == 6


class TestDangerousPatterns:
    def test_clean_command_no_patterns(self, tmp_path):
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert len(result.dangerous_patterns) == 0

    def test_sqlite3_import_flagged(self, tmp_path):
        code = "import sqlite3\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("Raw database import" in p and "sqlite3" in p for p in result.dangerous_patterns)

    def test_sqlalchemy_import_flagged(self, tmp_path):
        code = "from sqlalchemy import create_engine\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("Raw database import" in p and "sqlalchemy" in p for p in result.dangerous_patterns)

    def test_alembic_import_flagged(self, tmp_path):
        code = "import alembic\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("Raw database import" in p and "alembic" in p for p in result.dangerous_patterns)

    def test_command_data_repository_allowed(self, tmp_path):
        """Using CommandDataRepository is the sanctioned pattern — no raw DB flag."""
        code = "from repositories.command_data_repository import CommandDataRepository\nfrom db import SessionLocal\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert not any("Raw database import" in p for p in result.dangerous_patterns)

    def test_raw_db_suggests_data_repo(self, tmp_path):
        """Raw DB imports should suggest CommandDataRepository."""
        code = "import sqlite3\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("CommandDataRepository" in p for p in result.dangerous_patterns)

    def test_sql_create_table_flagged(self, tmp_path):
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "CREATE TABLE users (id INT)"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("SQL mutation" in p for p in result.dangerous_patterns)

    def test_sql_drop_table_flagged(self, tmp_path):
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "DROP TABLE secrets"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("SQL mutation" in p for p in result.dangerous_patterns)

    def test_sql_alter_table_flagged(self, tmp_path):
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "ALTER TABLE commands ADD COLUMN x INT"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("SQL mutation" in p for p in result.dangerous_patterns)

    def test_sql_select_not_flagged(self, tmp_path):
        """SELECT queries are not mutations — not flagged."""
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "SELECT * FROM my_data"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert not any("SQL mutation" in p for p in result.dangerous_patterns)

    def test_psycopg2_import_flagged(self, tmp_path):
        code = "import psycopg2\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("Raw database import" in p and "psycopg2" in p for p in result.dangerous_patterns)

    def test_cross_command_data_access(self, tmp_path):
        """Accessing another command's data via CommandDataRepository is flagged."""
        code = """\
from repositories.command_data_repository import CommandDataRepository
from db import SessionLocal
""" + VALID_COMMAND + """
    def extra(self):
        db = SessionLocal()
        repo = CommandDataRepository(db)
        data = repo.get("other_command", "some_key")
"""
        # Manifest says name is "test_cmd", but code accesses "other_command"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert any("Cross-command data access" in p for p in result.dangerous_patterns)

    def test_own_command_data_access_ok(self, tmp_path):
        """Accessing own data via CommandDataRepository is not flagged."""
        code = """\
from repositories.command_data_repository import CommandDataRepository
from db import SessionLocal
""" + VALID_COMMAND + """
    def extra(self):
        db = SessionLocal()
        repo = CommandDataRepository(db)
        data = repo.get("test_cmd", "some_key")
"""
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert not any("Cross-command data access" in p for p in result.dangerous_patterns)


class TestManifestValidation:
    def test_invalid_semver(self, tmp_path):
        manifest = {"name": "test", "description": "test", "version": "abc"}
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("semver" in e for e in result.errors)

    def test_unknown_category_warns(self, tmp_path):
        manifest = {"name": "test", "description": "test", "version": "1.0.0", "categories": ["bogus"]}
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True  # warnings don't fail
        assert any("Unknown category" in w for w in result.warnings)

    def test_valid_categories(self, tmp_path):
        manifest = {"name": "test", "description": "test", "version": "1.0.0", "categories": ["weather", "utilities"]}
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert len(result.warnings) == 0

    def test_unknown_param_type_warns(self, tmp_path):
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "parameters": [{"name": "x", "param_type": "invalid_type"}],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert any("param_type" in w for w in result.warnings)

    def test_unknown_secret_scope_warns(self, tmp_path):
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "secrets": [{"key": "API_KEY", "scope": "global"}],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert any("scope" in w for w in result.warnings)

    def test_valid_secret_scopes(self, tmp_path):
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "secrets": [{"key": "API_KEY", "scope": "integration"}],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert not any("scope" in w for w in result.warnings)


VALID_AGENT = """\
from jarvis_command_sdk import IJarvisAgent

class TestAgent(IJarvisAgent):
    @property
    def name(self):
        return "test_agent"

    @property
    def description(self):
        return "A test agent"

    @property
    def schedule(self):
        return {"interval_seconds": 60}

    @property
    def required_secrets(self):
        return []

    async def run(self):
        pass

    def get_context_data(self):
        return {}
"""

VALID_PROTOCOL = """\
from jarvis_command_sdk import IJarvisDeviceProtocol

class TestProtocol(IJarvisDeviceProtocol):
    @property
    def protocol_name(self):
        return "test_proto"

    @property
    def supported_domains(self):
        return ["light"]

    async def discover(self):
        return []

    async def control(self, entity_id, action, params=None):
        pass

    async def get_state(self, entity_id):
        return {}
"""


def _make_bundle_repo(tmp_path: Path, components: list[dict], manifest_extra: dict | None = None) -> Path:
    """Create a bundle repo with components."""
    repo = tmp_path / "repo"
    repo.mkdir()

    manifest = {
        "schema_version": 1,
        "name": "test_bundle",
        "description": "Test bundle",
        "version": "1.0.0",
        "components": components,
        **(manifest_extra or {}),
    }
    (repo / "jarvis_package.yaml").write_text(yaml.dump(manifest))
    (repo / "README.md").write_text("# Test")
    (repo / "LICENSE").write_text("MIT")

    return repo


class TestBundleAnalysis:
    def test_valid_bundle_command_and_agent(self, tmp_path):
        """Bundle with valid command + valid agent passes."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "turn_lights", "path": "commands/turn_lights/command.py"},
            {"type": "agent", "name": "home_state", "path": "agents/home_state/agent.py"},
        ])
        # Create component files
        (repo / "commands" / "turn_lights").mkdir(parents=True)
        (repo / "commands" / "turn_lights" / "command.py").write_text(VALID_COMMAND)
        (repo / "agents" / "home_state").mkdir(parents=True)
        (repo / "agents" / "home_state" / "agent.py").write_text(VALID_AGENT)

        result = run_static_analysis(repo)
        assert result.passed is True
        assert len(result.errors) == 0

    def test_bundle_missing_agent_method(self, tmp_path):
        """Bundle with agent missing required method fails."""
        incomplete_agent = """\
from jarvis_command_sdk import IJarvisAgent

class BadAgent(IJarvisAgent):
    @property
    def name(self):
        return "bad"

    @property
    def description(self):
        return "bad"
"""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "agent", "name": "bad_agent", "path": "agents/bad/agent.py"},
        ])
        (repo / "agents" / "bad").mkdir(parents=True)
        (repo / "agents" / "bad" / "agent.py").write_text(incomplete_agent)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("missing required" in e.lower() for e in result.errors)

    def test_bundle_valid_protocol(self, tmp_path):
        """Bundle with valid device protocol passes."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "device_protocol", "name": "test_proto", "path": "protocols/test/protocol.py"},
        ])
        (repo / "protocols" / "test").mkdir(parents=True)
        (repo / "protocols" / "test" / "protocol.py").write_text(VALID_PROTOCOL)

        result = run_static_analysis(repo)
        assert result.passed is True

    def test_bundle_missing_component_file(self, tmp_path):
        """Bundle with missing component file fails."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "ghost", "path": "commands/ghost/command.py"},
        ])
        # Don't create the file

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("not found" in e for e in result.errors)

    def test_bundle_dangerous_pattern_in_agent(self, tmp_path):
        """Hard-fail primitives in bundle components reject the whole bundle."""
        dangerous_agent = (
            "import subprocess\n"
            + VALID_AGENT
            + "\n    def extra(self):\n        subprocess.run(['ls'])\n"
        )
        repo = _make_bundle_repo(tmp_path, [
            {"type": "agent", "name": "evil", "path": "agents/evil/agent.py"},
        ])
        (repo / "agents" / "evil").mkdir(parents=True)
        (repo / "agents" / "evil" / "agent.py").write_text(dangerous_agent)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.run" in e for e in result.errors)
        assert any("subprocess" in p for p in result.dangerous_patterns)

    def test_v1_compat_no_components(self, tmp_path):
        """Repo with no components field still analyzed as single command."""
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert result.passed is True

    def test_agent_only_repo_inferred(self, tmp_path):
        """Repo with just agents/<name>/agent.py is detected as agent component."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "jarvis_command.yaml").write_text(yaml.dump({
            "name": "my_agent_pkg", "description": "Agent only", "version": "1.0.0",
        }))
        (repo / "README.md").write_text("# Test")
        (repo / "LICENSE").write_text("MIT")
        (repo / "agents" / "my_watcher").mkdir(parents=True)
        (repo / "agents" / "my_watcher" / "agent.py").write_text(VALID_AGENT)

        result = run_static_analysis(repo)
        assert result.passed is True
        assert len(result.errors) == 0

    def test_bundle_shared_dir_shadows_builtin_warns(self, tmp_path):
        """Shared dirs named 'services' or 'utils' warn about shadowing."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "my_cmd", "path": "commands/my_cmd/command.py"},
        ])
        (repo / "commands" / "my_cmd").mkdir(parents=True)
        (repo / "commands" / "my_cmd" / "command.py").write_text(VALID_COMMAND)
        # Add a shared dir that shadows a node builtin
        (repo / "services").mkdir()
        (repo / "services" / "my_service.py").write_text("def helper(): pass\n")

        result = run_static_analysis(repo)
        assert result.passed is True  # warning, not error
        assert any("shadows" in w.lower() for w in result.warnings)

    def test_bundle_shared_dir_safe_name_no_warning(self, tmp_path):
        """Shared dirs with unique names don't trigger shadow warning."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "my_cmd", "path": "commands/my_cmd/command.py"},
        ])
        (repo / "commands" / "my_cmd").mkdir(parents=True)
        (repo / "commands" / "my_cmd" / "command.py").write_text(VALID_COMMAND)
        # Add a safely-named shared dir
        (repo / "lifx_shared").mkdir()
        (repo / "lifx_shared" / "helpers.py").write_text("def helper(): pass\n")

        result = run_static_analysis(repo)
        assert not any("shadows" in w.lower() for w in result.warnings)

    def test_jarvis_package_yaml_preferred(self, tmp_path):
        """jarvis_package.yaml is preferred over jarvis_command.yaml."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "README.md").write_text("# Test")
        (repo / "LICENSE").write_text("MIT")

        # Create bundle manifest
        (repo / "agents" / "test").mkdir(parents=True)
        (repo / "agents" / "test" / "agent.py").write_text(VALID_AGENT)
        manifest = {
            "schema_version": 1,
            "name": "test_pkg",
            "description": "Test",
            "version": "1.0.0",
            "components": [
                {"type": "agent", "name": "test_agent", "path": "agents/test/agent.py"},
            ],
        }
        (repo / "jarvis_package.yaml").write_text(yaml.dump(manifest))

        result = run_static_analysis(repo)
        assert result.passed is True


class TestStaticAnalysisResult:
    def test_to_dict(self):
        result = StaticAnalysisResult(
            passed=True,
            warnings=["w1"],
            errors=[],
            dangerous_patterns=["eval()"],
        )
        d = result.to_dict()
        assert d["passed"] is True
        assert d["warnings"] == ["w1"]
        assert d["dangerous_patterns"] == ["eval()"]
        assert d["checks_passed"] == 8  # all checks passed

    def test_checks_passed_decreases_with_errors(self):
        result = StaticAnalysisResult(
            passed=False,
            errors=["e1", "e2"],
        )
        assert result.to_dict()["checks_passed"] == 6

    def test_checks_passed_decreases_with_hard_fail_errors(self, tmp_path):
        """A single hard-fail primitive lowers checks_passed from 8 → 7."""
        code = VALID_COMMAND + "\n    def extra(self):\n        eval('x')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert result.to_dict()["checks_passed"] == 7


# ── Jarvis package dependencies (transitive inheritance) ──────────────────


TRANSITIVE_PROTOCOL = """\
from nest import NestProtocol

class ExtendedNest(NestProtocol):
    @property
    def protocol_name(self):
        return "nest_extended"

    @property
    def supported_domains(self):
        return ["climate", "energy"]

    async def discover(self):
        return []

    async def control(self, entity_id, action, params=None):
        pass

    async def get_state(self, entity_id):
        return {}
"""


class TestJarvisDependencyAnalysis:
    def test_transitive_inheritance_accepted_with_deps(self, tmp_path):
        """Protocol inheriting from dependency class passes with jarvis_dependencies."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "device_protocol", "name": "nest_ext", "path": "device_families/nest_ext/protocol.py"},
        ], manifest_extra={"jarvis_dependencies": ["nest"]})

        (repo / "device_families" / "nest_ext").mkdir(parents=True)
        (repo / "device_families" / "nest_ext" / "protocol.py").write_text(TRANSITIVE_PROTOCOL)

        result = run_static_analysis(repo)
        assert result.passed is True
        assert any("transitive inheritance" in w.lower() for w in result.warnings)

    def test_transitive_inheritance_rejected_without_deps(self, tmp_path):
        """Same code without jarvis_dependencies declaration fails."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "device_protocol", "name": "nest_ext", "path": "device_families/nest_ext/protocol.py"},
        ])
        # No jarvis_dependencies

        (repo / "device_families" / "nest_ext").mkdir(parents=True)
        (repo / "device_families" / "nest_ext" / "protocol.py").write_text(TRANSITIVE_PROTOCOL)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("IJarvisDeviceProtocol" in e for e in result.errors)

    def test_required_methods_still_checked_with_deps(self, tmp_path):
        """Missing required methods are still flagged even with jarvis_dependencies."""
        incomplete_code = """\
from nest import NestProtocol

class IncompleteNest(NestProtocol):
    @property
    def protocol_name(self):
        return "incomplete"
"""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "device_protocol", "name": "bad_ext", "path": "device_families/bad/protocol.py"},
        ], manifest_extra={"jarvis_dependencies": ["nest"]})

        (repo / "device_families" / "bad").mkdir(parents=True)
        (repo / "device_families" / "bad" / "protocol.py").write_text(incomplete_code)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("missing required" in e.lower() for e in result.errors)

    def test_no_class_at_all_fails_even_with_deps(self, tmp_path):
        """File with no class definitions fails even with jarvis_dependencies."""
        repo = _make_bundle_repo(tmp_path, [
            {"type": "device_protocol", "name": "empty", "path": "device_families/empty/protocol.py"},
        ], manifest_extra={"jarvis_dependencies": ["nest"]})

        (repo / "device_families" / "empty").mkdir(parents=True)
        (repo / "device_families" / "empty" / "protocol.py").write_text("# empty file\ndef helper(): pass\n")

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("no class definition" in e.lower() for e in result.errors)


# ── Hard-fail primitives (eval/exec/os.system/os.popen/os.exec*/os.spawn*/subprocess.*) ──

import pytest


def _command_with_extra(body_line: str) -> str:
    """Helper: append a single body line inside a method on VALID_COMMAND."""
    return VALID_COMMAND + f"\n    def extra(self):\n        {body_line}\n"


class TestHardFailPrimitives:
    # Direct calls (replaces warn-only tests)

    def test_eval_hard_fails(self, tmp_path):
        repo = _make_repo(tmp_path, _command_with_extra("eval('x')"))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("eval" in e for e in result.errors)

    def test_exec_hard_fails(self, tmp_path):
        repo = _make_repo(tmp_path, _command_with_extra("exec('x = 1')"))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("exec" in e for e in result.errors)

    def test_os_system_hard_fails(self, tmp_path):
        code = VALID_COMMAND + "\n    def extra(self):\n        import os\n        os.system('ls')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("os.system" in e for e in result.errors)

    def test_subprocess_run_hard_fails(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        subprocess.run(['ls'])\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.run" in e for e in result.errors)

    # subprocess.* family coverage (broad rule per Alex's resolution)

    def test_subprocess_call_hard_fails(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        subprocess.call(['ls'])\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.call" in e for e in result.errors)

    def test_subprocess_popen_hard_fails(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        subprocess.Popen(['ls'])\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.Popen" in e for e in result.errors)

    def test_subprocess_check_call_hard_fails(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        subprocess.check_call(['ls'])\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.check_call" in e for e in result.errors)

    def test_subprocess_check_output_hard_fails(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        subprocess.check_output(['ls'])\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("subprocess.check_output" in e for e in result.errors)

    # os.* family coverage

    def test_os_popen_hard_fails(self, tmp_path):
        code = VALID_COMMAND + "\n    def extra(self):\n        import os\n        os.popen('ls')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("os.popen" in e for e in result.errors)

    @pytest.mark.parametrize("fn", [
        "os.exec", "os.execl", "os.execle", "os.execlp",
        "os.execv", "os.execve", "os.execvp", "os.execvpe",
    ])
    def test_os_exec_family_hard_fails(self, tmp_path, fn):
        code = VALID_COMMAND + f"\n    def extra(self):\n        import os\n        {fn}('/bin/ls', 'ls')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any(fn in e for e in result.errors)

    @pytest.mark.parametrize("fn", ["os.spawn", "os.spawnl", "os.spawnle"])
    def test_os_spawn_family_hard_fails(self, tmp_path, fn):
        code = VALID_COMMAND + f"\n    def extra(self):\n        import os\n        {fn}(0, '/bin/ls')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any(fn in e for e in result.errors)

    # Negative-space regression guards (warn-only must still pass)

    def test_compile_stays_warn_only(self, tmp_path):
        code = VALID_COMMAND + "\n    def extra(self):\n        compile('1', '<s>', 'exec')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert any("compile" in p for p in result.dangerous_patterns)
        assert not any("Disallowed primitive" in e for e in result.errors)

    def test_dunder_import_stays_warn_only(self, tmp_path):
        code = VALID_COMMAND + "\n    def extra(self):\n        __import__('os')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert any("__import__" in p for p in result.dangerous_patterns)
        assert not any("Disallowed primitive" in e for e in result.errors)

    def test_sql_mutation_stays_warn_only(self, tmp_path):
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "CREATE TABLE users (id INT)"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert any("SQL mutation" in p for p in result.dangerous_patterns)
        assert not any("Disallowed primitive" in e for e in result.errors)

    # Bundle-level rejection

    def test_bundle_hard_fail_in_one_component_rejects_bundle(self, tmp_path):
        evil_agent = VALID_AGENT + "\n    def extra(self):\n        eval('x')\n"
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "good", "path": "commands/good/command.py"},
            {"type": "agent", "name": "evil", "path": "agents/evil/agent.py"},
        ])
        (repo / "commands" / "good").mkdir(parents=True)
        (repo / "commands" / "good" / "command.py").write_text(VALID_COMMAND)
        (repo / "agents" / "evil").mkdir(parents=True)
        (repo / "agents" / "evil" / "agent.py").write_text(evil_agent)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("eval" in e for e in result.errors)

    def test_bundle_multiple_hard_fails_all_surfaced(self, tmp_path):
        evil_cmd = VALID_COMMAND + "\n    def extra(self):\n        eval('x')\n"
        evil_agent = VALID_AGENT + "\n    def extra(self):\n        import subprocess\n        subprocess.run(['ls'])\n"
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "evil_cmd", "path": "commands/evil_cmd/command.py"},
            {"type": "agent", "name": "evil_agent", "path": "agents/evil_agent/agent.py"},
        ])
        (repo / "commands" / "evil_cmd").mkdir(parents=True)
        (repo / "commands" / "evil_cmd" / "command.py").write_text(evil_cmd)
        (repo / "agents" / "evil_agent").mkdir(parents=True)
        (repo / "agents" / "evil_agent" / "agent.py").write_text(evil_agent)

        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("eval" in e for e in result.errors)
        assert any("subprocess.run" in e for e in result.errors)
