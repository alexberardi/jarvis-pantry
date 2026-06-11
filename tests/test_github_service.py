"""Tests for GitHub repository service."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from app.services.github_service import (
    clone_repo,
    validate_structure,
    read_command_source,
    cleanup_repo,
    RepoValidationError,
)


class TestCloneRepo:
    """clone_repo returns (repo_dir, head_sha) — the SHA pins what was validated."""

    _SHA = "0123456789abcdef0123456789abcdef01234567"

    def _result(self, returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    @patch("app.services.github_service.subprocess.run")
    def test_returns_path_and_sha(self, mock_run):
        mock_run.side_effect = [
            self._result(),                          # git clone
            self._result(stdout=f"{self._SHA}\n"),   # git rev-parse HEAD
        ]
        repo_dir, sha = clone_repo("https://github.com/test/repo")
        assert repo_dir.name == "repo"
        assert sha == self._SHA
        # rev-parse ran inside the fresh checkout
        rev_parse_call = mock_run.call_args_list[1]
        assert rev_parse_call.args[0] == ["git", "rev-parse", "HEAD"]
        assert rev_parse_call.kwargs["cwd"] == repo_dir
        cleanup_repo(repo_dir)

    @patch("app.services.github_service.subprocess.run")
    def test_rev_parse_failure_returns_none_sha(self, mock_run):
        """A missing SHA (e.g. non-git archive download) never blocks the clone."""
        mock_run.side_effect = [
            self._result(),
            self._result(returncode=128, stderr="fatal: not a git repository"),
        ]
        repo_dir, sha = clone_repo("https://github.com/test/repo")
        assert repo_dir.name == "repo"
        assert sha is None
        cleanup_repo(repo_dir)

    @patch("app.services.github_service.subprocess.run")
    def test_rev_parse_timeout_returns_none_sha(self, mock_run):
        mock_run.side_effect = [
            self._result(),
            subprocess.TimeoutExpired(cmd=["git", "rev-parse", "HEAD"], timeout=10),
        ]
        repo_dir, sha = clone_repo("https://github.com/test/repo")
        assert sha is None
        cleanup_repo(repo_dir)

    @patch("app.services.github_service.subprocess.run")
    def test_empty_rev_parse_output_returns_none_sha(self, mock_run):
        mock_run.side_effect = [self._result(), self._result(stdout="\n")]
        repo_dir, sha = clone_repo("https://github.com/test/repo")
        assert sha is None
        cleanup_repo(repo_dir)

    @patch("app.services.github_service.subprocess.run")
    def test_clone_failure_raises_without_rev_parse(self, mock_run):
        mock_run.return_value = self._result(returncode=128, stderr="fatal: not found")
        with pytest.raises(RepoValidationError, match="git clone failed"):
            clone_repo("https://github.com/test/missing")
        assert mock_run.call_count == 1

    @patch("app.services.github_service.subprocess.run")
    def test_tag_passed_to_clone(self, mock_run):
        mock_run.side_effect = [self._result(), self._result(stdout=self._SHA)]
        repo_dir, _sha = clone_repo("https://github.com/test/repo", tag="v1.0.0")
        clone_cmd = mock_run.call_args_list[0].args[0]
        assert "--branch" in clone_cmd and "v1.0.0" in clone_cmd
        cleanup_repo(repo_dir)


class TestValidateStructure:
    def _create_repo(self, tmp_path, files: dict[str, str] | None = None):
        repo = tmp_path / "repo"
        repo.mkdir()
        default_files = {
            "jarvis_command.yaml": yaml.dump({"name": "test", "description": "Test", "version": "1.0.0"}),
            "command.py": "# command",
            "README.md": "# Test",
            "LICENSE": "MIT",
        }
        if files:
            default_files.update(files)
        for name, content in default_files.items():
            (repo / name).write_text(content)
        return repo

    def test_valid_repo(self, tmp_path):
        repo = self._create_repo(tmp_path)
        manifest = validate_structure(repo)
        assert manifest["name"] == "test"
        assert manifest["version"] == "1.0.0"

    def test_missing_command_py(self, tmp_path):
        repo = self._create_repo(tmp_path)
        (repo / "command.py").unlink()
        with pytest.raises(RepoValidationError, match="command.py"):
            validate_structure(repo)

    def test_missing_readme(self, tmp_path):
        repo = self._create_repo(tmp_path)
        (repo / "README.md").unlink()
        with pytest.raises(RepoValidationError, match="README.md"):
            validate_structure(repo)

    def test_invalid_yaml(self, tmp_path):
        repo = self._create_repo(tmp_path, {"jarvis_command.yaml": "{{invalid"})
        with pytest.raises(RepoValidationError, match="Invalid YAML"):
            validate_structure(repo)

    def test_missing_name(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({"description": "no name", "version": "1.0"})
        })
        with pytest.raises(RepoValidationError, match="name"):
            validate_structure(repo)

    def test_missing_version(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({"name": "test", "description": "no version"})
        })
        with pytest.raises(RepoValidationError, match="version"):
            validate_structure(repo)

    def test_apt_packages_absent_is_accepted(self, tmp_path):
        """No apt_packages key in manifest is the default and must not break parsing."""
        repo = self._create_repo(tmp_path)
        manifest = validate_structure(repo)
        # Absent → key simply isn't materialized; node-side reader treats as empty.
        assert "apt_packages" not in manifest or manifest["apt_packages"] == []

    def test_apt_packages_empty_list_is_accepted(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({
                "name": "test", "description": "T", "version": "1.0.0",
                "apt_packages": [],
            }),
        })
        manifest = validate_structure(repo)
        assert manifest["apt_packages"] == []

    def test_apt_packages_list_of_strings_passes(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({
                "name": "test", "description": "T", "version": "1.0.0",
                "apt_packages": ["mpv", "ffmpeg"],
            }),
        })
        manifest = validate_structure(repo)
        assert manifest["apt_packages"] == ["mpv", "ffmpeg"]

    def test_apt_packages_null_normalized_to_empty_list(self, tmp_path):
        """A YAML-null entry is treated as 'no apt deps' rather than rejected —
        keeps manifests valid when authors comment out the list."""
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({
                "name": "test", "description": "T", "version": "1.0.0",
                "apt_packages": None,
            }),
        })
        manifest = validate_structure(repo)
        assert manifest["apt_packages"] == []

    def test_apt_packages_wrong_outer_type_rejected(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({
                "name": "test", "description": "T", "version": "1.0.0",
                "apt_packages": "mpv",  # str instead of list
            }),
        })
        with pytest.raises(RepoValidationError, match="apt_packages.*must be a list"):
            validate_structure(repo)

    def test_apt_packages_non_string_entry_rejected(self, tmp_path):
        repo = self._create_repo(tmp_path, {
            "jarvis_command.yaml": yaml.dump({
                "name": "test", "description": "T", "version": "1.0.0",
                "apt_packages": ["mpv", 42],
            }),
        })
        with pytest.raises(RepoValidationError, match="apt_packages.*entries must be strings"):
            validate_structure(repo)


class TestReadCommandSource:
    def test_reads_source(self, tmp_path):
        (tmp_path / "command.py").write_text("class MyCmd: pass")
        source = read_command_source(tmp_path)
        assert "class MyCmd" in source


class TestCleanupRepo:
    def test_cleanup(self, tmp_path):
        # Simulate the clone structure: tmpdir/repo
        clone_parent = tmp_path / "jarvis-store-abc123"
        repo = clone_parent / "repo"
        repo.mkdir(parents=True)
        (repo / "file.txt").write_text("test")

        cleanup_repo(repo)
        assert not clone_parent.exists()

    def test_cleanup_wrong_prefix_noop(self, tmp_path):
        """Doesn't delete if parent doesn't start with jarvis-store-."""
        repo = tmp_path / "other" / "repo"
        repo.mkdir(parents=True)
        cleanup_repo(repo)
        # tmp_path/other still exists because prefix check fails
        assert (tmp_path / "other").exists()
