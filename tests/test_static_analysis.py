"""Tests for static analysis service."""

from pathlib import Path

import pytest
import yaml

from app.services import static_analysis as static_analysis_mod
from app.services.apt_allowlist import AptAllowlist, AptAllowlistEntry
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

    def test_unknown_secret_scope_blocks_submission(self, tmp_path):
        # Invalid scope crashes the snapshot pipeline on the node, so the
        # Pantry rejects the submission rather than letting bad packages ship.
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "secrets": [{"key": "API_KEY", "scope": "global"}],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("scope" in e for e in result.errors)

    def test_valid_secret_scopes(self, tmp_path):
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "secrets": [
                {"key": "API_KEY", "scope": "integration"},
                {"key": "USER_TOKEN", "scope": "user"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert not any("scope" in w for w in result.warnings)
        assert not any("scope" in e for e in result.errors)

    def test_node_scope_now_blocks_submission(self, tmp_path):
        # "node" scope was collapsed into "integration"; manifests still
        # declaring it must be rejected because the node SDK validator will
        # raise on instantiation and break the snapshot for the whole device.
        manifest = {
            "name": "test", "description": "test", "version": "1.0.0",
            "secrets": [{"key": "API_KEY", "scope": "node"}],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("scope" in e and "node" in e for e in result.errors)


class TestPythonSourceSecretScope:
    """The manifest-only scope check misses packages that declare secrets
    inline via `JarvisSecret(...)` calls in Python source. Static analysis
    must AST-scan source files too — a `node` scope hardcoded in agent.py
    crashes the snapshot just as hard as one in the manifest.
    """

    def _command_with_inline_secret(self, scope_literal: str, positional: bool = True) -> str:
        if positional:
            secret_call = f'JarvisSecret("API_KEY", "An API key", "{scope_literal}", "string", is_sensitive=True)'
        else:
            secret_call = f'JarvisSecret("API_KEY", "An API key", scope="{scope_literal}", value_type="string", is_sensitive=True)'
        return f"""\
from jarvis_command_sdk import IJarvisCommand, JarvisSecret

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
        return [{secret_call}]

    @property
    def keywords(self):
        return ["test"]

    def run(self, request_info, **kwargs):
        return {{"status": "ok"}}

    def generate_prompt_examples(self):
        return []

    def generate_adapter_examples(self):
        return []
"""

    def test_inline_node_scope_positional_blocks_submission(self, tmp_path):
        repo = _make_repo(tmp_path, self._command_with_inline_secret("node", positional=True))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("JarvisSecret" in e and "node" in e for e in result.errors)

    def test_inline_node_scope_keyword_blocks_submission(self, tmp_path):
        repo = _make_repo(tmp_path, self._command_with_inline_secret("node", positional=False))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("JarvisSecret" in e and "node" in e for e in result.errors)

    def test_inline_unknown_scope_blocks_submission(self, tmp_path):
        repo = _make_repo(tmp_path, self._command_with_inline_secret("global"))
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("JarvisSecret" in e and "global" in e for e in result.errors)

    def test_inline_integration_scope_passes(self, tmp_path):
        repo = _make_repo(tmp_path, self._command_with_inline_secret("integration"))
        result = run_static_analysis(repo)
        assert not any("JarvisSecret" in e for e in result.errors)

    def test_inline_user_scope_passes(self, tmp_path):
        repo = _make_repo(tmp_path, self._command_with_inline_secret("user"))
        result = run_static_analysis(repo)
        assert not any("JarvisSecret" in e for e in result.errors)


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


# ── Structured findings (rejection-reason taxonomy, #18) ────────────────


def _findings_with_code(result, code: str) -> list:
    """Helper: all findings across errors + warnings with the given reason_code."""
    return [f for f in result.findings if f.reason_code == code]


class TestStructuredFindings:
    """Tests for the new Finding-based structured rejection shape (#18).

    These run alongside the existing flat-string assertions in TestDangerousPatterns
    and TestManifestValidation. Existing assertions are kept as back-compat coverage;
    the new envelope is the contract clients will rely on going forward.
    """

    def test_eval_emits_structured_finding(self, tmp_path):
        code = VALID_COMMAND + "\n    def extra(self):\n        eval('x')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_disallowed_primitive")
        assert len(matches) >= 1
        f = next(m for m in matches if m.primitive == "eval")
        assert f.severity == "warning"
        assert f.file is not None and f.file.endswith("command.py")
        assert f.line is not None and f.line > 30
        assert f.snippet is not None and "eval" in f.snippet
        assert f.doc_url.startswith("https://docs.jarvisautomation.dev/")

    def test_subprocess_import_emits_structured_finding(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_disallowed_primitive")
        f = next(m for m in matches if m.primitive == "subprocess")
        assert f.file is not None and f.file.endswith("command.py")
        assert f.line == 1
        assert f.snippet is not None and "subprocess" in f.snippet

    def test_sqlite3_import_emits_structured_finding(self, tmp_path):
        code = "import sqlite3\n" + VALID_COMMAND
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_raw_db_import")
        assert len(matches) >= 1
        assert any("sqlite3" in (m.primitive or "") for m in matches)

    def test_sql_mutation_emits_structured_finding(self, tmp_path):
        code = VALID_COMMAND + '\n    def extra(self):\n        q = "CREATE TABLE x (id INT)"\n'
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_sql_mutation")
        assert len(matches) >= 1
        # snippet should carry the SQL keyword that tripped detection
        assert any("CREATE TABLE" in (m.snippet or m.value or "").upper() for m in matches)

    def test_invalid_semver_emits_structured_finding(self, tmp_path):
        manifest = {"name": "test", "description": "test", "version": "abc"}
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        matches = _findings_with_code(result, "manifest_bad_semver")
        assert len(matches) == 1
        f = matches[0]
        assert f.severity == "error"
        assert f.value == "abc"
        assert f.file is None  # manifest-level — no file/line
        assert f.line is None
        assert f.doc_url.endswith("#manifest_bad_semver")

    def test_unknown_category_emits_structured_warning(self, tmp_path):
        manifest = {"name": "test", "description": "test", "version": "1.0.0", "categories": ["bogus"]}
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True
        matches = _findings_with_code(result, "manifest_unknown_category")
        assert len(matches) == 1
        assert matches[0].severity == "warning"
        assert matches[0].value == "bogus"

    def test_missing_required_method_emits_structured_finding(self, tmp_path):
        code = """\
from jarvis_command_sdk import IJarvisCommand

class Incomplete(IJarvisCommand):
    pass
"""
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "manifest_missing_required_field")
        assert len(matches) >= 1
        assert all(m.severity == "error" for m in matches)

    def test_cross_command_data_access_emits_structured_finding(self, tmp_path):
        code = """\
from repositories.command_data_repository import CommandDataRepository
from db import SessionLocal
""" + VALID_COMMAND + """
    def extra(self):
        db = SessionLocal()
        repo = CommandDataRepository(db)
        data = repo.get("other_command", "some_key")
"""
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_cross_command_access")
        assert len(matches) >= 1
        assert matches[0].value == "other_command"

    def test_multiple_findings_in_one_run_are_all_captured(self, tmp_path):
        code = "import subprocess\n" + VALID_COMMAND + "\n    def extra(self):\n        eval('x')\n"
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_disallowed_primitive")
        primitives = {m.primitive for m in matches}
        assert "subprocess" in primitives
        assert "eval" in primitives

    def test_reason_codes_envelope_deduplicates(self, tmp_path):
        code = (
            VALID_COMMAND
            + "\n    def extra(self):\n        eval('a')\n        eval('b')\n"
        )
        repo = _make_repo(tmp_path, code)
        result = run_static_analysis(repo)
        d = result.to_dict()
        # reason_codes is a deduplicated set-like list
        assert d["reason_codes"].count("static_analysis_disallowed_primitive") == 1

    def test_clean_command_has_no_findings(self, tmp_path):
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert result.findings == []
        assert result.to_dict()["reason_codes"] == []

    def test_bundle_finding_carries_component_file_path(self, tmp_path):
        repo = _make_bundle_repo(tmp_path, [
            {"type": "command", "name": "turn_lights", "path": "commands/turn_lights/command.py"},
        ])
        (repo / "commands" / "turn_lights").mkdir(parents=True)
        bad_code = VALID_COMMAND + "\n    def extra(self):\n        eval('x')\n"
        (repo / "commands" / "turn_lights" / "command.py").write_text(bad_code)

        result = run_static_analysis(repo)
        matches = _findings_with_code(result, "static_analysis_disallowed_primitive")
        f = next(m for m in matches if m.primitive == "eval")
        assert f.file == "commands/turn_lights/command.py"


class TestStaticAnalysisResultNewEnvelope:
    """The to_dict() envelope now includes findings + reason_codes alongside legacy keys."""

    def test_to_dict_includes_findings_and_reason_codes(self):
        from app.services.rejection_codes import Finding, doc_url
        finding = Finding(
            reason_code="manifest_bad_semver",
            severity="error",
            value="abc",
            doc_url=doc_url("manifest_bad_semver"),
        )
        result = StaticAnalysisResult(passed=False)
        result.findings.append(finding)
        result.errors.append("Invalid semver version: abc")  # legacy mirror
        d = result.to_dict()
        assert "findings" in d
        assert "reason_codes" in d
        assert d["reason_codes"] == ["manifest_bad_semver"]
        assert d["findings"][0]["reason_code"] == "manifest_bad_semver"
        assert d["findings"][0]["value"] == "abc"

    def test_to_dict_keeps_legacy_keys_for_back_compat(self):
        # Existing consumers (old DB rows, old client parsers) still see the flat-string keys.
        result = StaticAnalysisResult(passed=True, warnings=["w1"], dangerous_patterns=["eval()"])
        d = result.to_dict()
        assert d["warnings"] == ["w1"]
        assert d["dangerous_patterns"] == ["eval()"]



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


# ── Apt allow-list validation (#16) ───────────────────────────────────────

def _fake_allowlist(*names: str) -> AptAllowlist:
    """Build an injectable AptAllowlist for tests (no shipped YAML required)."""
    entries = [
        AptAllowlistEntry(name=n, reason="testing", added_by="test", added_at="2026-05-18")
        for n in names
    ]
    return AptAllowlist(entries=entries)


def _inject_allowlist(monkeypatch, *names: str) -> None:
    monkeypatch.setattr(static_analysis_mod, "get_allowlist", lambda: _fake_allowlist(*names))


class TestAptAllowlistValidation:
    def test_manifest_with_on_list_apt_packages_passes(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch, "mpv", "ffmpeg")
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["mpv", "ffmpeg"],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert not any("apt" in e.lower() for e in result.errors)
        assert not any("allowlist" in e.lower() for e in result.errors)

    def test_manifest_with_empty_apt_packages_skips_check(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)  # empty allow-list — irrelevant, no packages requested
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": [],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert not any("apt" in e.lower() for e in result.errors)

    def test_manifest_with_no_apt_packages_field_skips_check(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)
        # _make_repo's default manifest has no apt_packages field at all.
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert not any("apt" in e.lower() for e in result.errors)

    def test_mixed_apt_packages_full_reject_no_partial_accept(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch, "mpv")
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["mpv", "postgresql-server"],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        offending = [e for e in result.errors if "postgresql-server" in e]
        assert len(offending) == 1
        # mpv is on the list — must NOT appear in errors.
        assert not any("'mpv'" in e or '"mpv"' in e for e in result.errors)

    def test_all_off_list_packages_each_get_their_own_error(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)  # nothing allowed
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["postgresql-server", "redis-server", "nginx"],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        for pkg in ("postgresql-server", "redis-server", "nginx"):
            assert any(pkg in e for e in result.errors), f"missing rejection for {pkg}"

    @pytest.mark.parametrize("pkg", [
        "mpv", "vlc", "ffmpeg", "alsa-utils", "sox", "mopidy",
        "pulseaudio", "pipewire-pulse", "bluez", "yt-dlp", "imagemagick",
    ])
    def test_seed_list_packages_individually_accepted(self, tmp_path, pkg):
        """Use the real shipped allowlist (no monkeypatch) — sanity-check the seed file."""
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": [pkg],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True, f"seed package {pkg} was rejected; errors={result.errors}"

    def test_off_list_error_includes_request_url(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["postgresql-server"],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        offending = [e for e in result.errors if "postgresql-server" in e]
        assert len(offending) == 1
        err = offending[0]
        assert "github.com/alexberardi/jarvis-pantry/issues/new" in err
        assert "apt-package-request" in err
        assert "postgresql-server" in err

    def test_off_list_finding_carries_reason_code(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["postgresql-server"],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        codes = result.reason_codes
        assert "apt_package_not_on_allowlist" in codes
        finding = next(
            f for f in result.findings
            if f.reason_code == "apt_package_not_on_allowlist" and f.value == "postgresql-server"
        )
        assert finding.severity == "error"
        assert finding.primitive == "apt"

    def test_apt_packages_not_a_list_clean_rejection(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch, "mpv")
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": "mpv",  # wrong type — string instead of list
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any(
            "apt_packages" in e and ("list" in e.lower() or "type" in e.lower())
            for e in result.errors
        )

    def test_apt_packages_with_non_string_entry(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch, "mpv")
        manifest = {
            "name": "test_cmd",
            "description": "Test command",
            "version": "1.0.0",
            "apt_packages": ["mpv", 42],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("42" in e for e in result.errors)

    def test_allowlist_check_does_not_run_when_manifest_parse_fails(self, tmp_path, monkeypatch):
        _inject_allowlist(monkeypatch)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "command.py").write_text(VALID_COMMAND)
        # Deliberately malformed YAML
        (repo / "jarvis_command.yaml").write_text("name: test\n  bad: : :\n")
        (repo / "README.md").write_text("# Test")
        (repo / "LICENSE").write_text("MIT")
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("Failed to parse manifest" in e for e in result.errors)
        # No apt-allowlist finding should be emitted when the manifest never
        # parsed. Match via reason codes rather than substring on the message,
        # because the parse-error message embeds the YAML path, and pytest's
        # tmp_path includes the test name (which contains "allowlist").
        assert "apt_package_not_on_allowlist" not in result.reason_codes


# --- post_install allow-list ----------------------------------------------


def _fake_post_install_allowlist(*pairs: tuple[str, str]):
    """Build a PostInstallAllowlist from (op_type, target) tuples."""
    from app.services.post_install_allowlist import (
        PostInstallAllowlist, PostInstallAllowlistEntry,
    )
    entries = [
        PostInstallAllowlistEntry(
            op_type=op, target=tgt, reason="test", added_by="test", added_at="test",
        )
        for op, tgt in pairs
    ]
    return PostInstallAllowlist(entries=entries)


def _inject_post_install_allowlist(monkeypatch, *pairs: tuple[str, str]) -> None:
    monkeypatch.setattr(
        static_analysis_mod,
        "get_post_install_allowlist",
        lambda: _fake_post_install_allowlist(*pairs),
    )


class TestPostInstallAllowlistValidation:
    """Submissions can only declare post_install ops whose (op_type, target)
    pair is on the curated allow-list. This is the Pantry-side safety gate
    that decides which services a package is allowed to configure."""

    def test_manifest_with_no_post_install_skips_check(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(monkeypatch)
        repo = _make_repo(tmp_path, VALID_COMMAND)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert not any("post_install" in e for e in result.errors)

    def test_manifest_with_empty_post_install_skips_check(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(monkeypatch)
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True

    def test_on_list_op_passes(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(
            monkeypatch,
            ("configure_systemd_service", "shairport-sync"),
            ("set_config_file_value", "/etc/shairport-sync.conf"),
        )
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                {"type": "configure_systemd_service", "service": "shairport-sync",
                 "run_as": "jarvis_user"},
                {"type": "set_config_file_value",
                 "file": "/etc/shairport-sync.conf", "format": "libconfig",
                 "section": "general", "key": "output_backend", "value": "pa"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is True
        assert not any("post_install" in e for e in result.errors)

    def test_off_list_service_rejected(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(
            monkeypatch, ("configure_systemd_service", "shairport-sync"),
        )
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                {"type": "configure_systemd_service", "service": "sshd"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("sshd" in e and "allow-list" in e for e in result.errors)
        assert "post_install_op_not_on_allowlist" in result.reason_codes

    def test_off_list_file_rejected(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(
            monkeypatch, ("set_config_file_value", "/etc/shairport-sync.conf"),
        )
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                {"type": "set_config_file_value", "file": "/etc/passwd",
                 "format": "libconfig", "section": "x", "key": "y", "value": "z"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert any("/etc/passwd" in e for e in result.errors)

    def test_unknown_op_type_rejected(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(monkeypatch)
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                {"type": "exec_arbitrary_script", "command": "rm -rf /"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert "post_install_op_unknown_type" in result.reason_codes

    def test_missing_target_field_rejected(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(
            monkeypatch, ("configure_systemd_service", "shairport-sync"),
        )
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                # Missing required "service" field.
                {"type": "configure_systemd_service", "run_as": "jarvis_user"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert "post_install_op_missing_target" in result.reason_codes

    def test_post_install_not_a_list_clean_rejection(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(monkeypatch)
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": "oops",  # not a list
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        assert "manifest_invalid_field_type" in result.reason_codes

    def test_mixed_ops_full_reject_no_partial_accept(self, tmp_path, monkeypatch):
        _inject_post_install_allowlist(
            monkeypatch, ("configure_systemd_service", "shairport-sync"),
        )
        manifest = {
            "name": "t", "description": "T", "version": "1.0.0",
            "post_install": [
                {"type": "configure_systemd_service", "service": "shairport-sync"},
                {"type": "configure_systemd_service", "service": "sshd"},
            ],
        }
        repo = _make_repo(tmp_path, VALID_COMMAND, manifest)
        result = run_static_analysis(repo)
        assert result.passed is False
        # Only the off-list entry should be flagged.
        sshd_errs = [e for e in result.errors if "sshd" in e]
        assert len(sshd_errs) == 1

    def test_seed_allowlist_actually_loads(self):
        """The on-disk YAML loads cleanly and includes our seed entries —
        guards against the file going missing or the format drifting."""
        from app.services.post_install_allowlist import (
            DEFAULT_ALLOWLIST_PATH, load_allowlist,
        )
        al = load_allowlist(DEFAULT_ALLOWLIST_PATH)
        assert al.is_allowed("configure_systemd_service", "shairport-sync")
        assert al.is_allowed("set_config_file_value", "/etc/shairport-sync.conf")
        assert not al.is_allowed("configure_systemd_service", "sshd")
        assert not al.is_allowed("set_config_file_value", "/etc/passwd")
