"""Forge must not write LLM-chosen filenames outside its temp dir.

Round-2 CRITICAL: POST /v1/forge/generate ran _validate_generated_files, which
wrote each file to ``tmpdir / f.filename`` with no sanitization. Because the
filename comes from the (untrusted) LLM, an absolute or ``../`` filename escaped
the temp dir — an unauthenticated arbitrary file write as root on the
internet-facing Fly host.
"""
import tempfile as _tempfile

import pytest

from app.services import forge_generator as fg
from app.services.forge_generator import GeneratedFile


class _EmptyResult:
    errors: list = []
    warnings: list = []
    dangerous_patterns: list = []


@pytest.fixture
def controlled_tmp(tmp_path, monkeypatch):
    validate_dir = tmp_path / "validate"
    validate_dir.mkdir()
    monkeypatch.setattr(_tempfile, "mkdtemp", lambda prefix="": str(validate_dir))
    # Don't run the heavy real static analyzer for this test.
    monkeypatch.setattr(
        "app.services.static_analysis.run_static_analysis",
        lambda d: _EmptyResult(),
    )
    return tmp_path


def test_traversal_filename_cannot_escape_tmpdir(controlled_tmp):
    escaped = controlled_tmp / "escaped.txt"
    files = [GeneratedFile(filename="../escaped.txt", content="PWNED", language="python")]

    result = fg._validate_generated_files(files)

    assert not escaped.exists(), "path-traversal filename escaped the temp dir"
    blob = " ".join(result.errors + result.dangerous_patterns).lower()
    assert "unsafe" in blob or "escaped.txt" in blob


def test_absolute_filename_cannot_escape(controlled_tmp, tmp_path):
    target = tmp_path / "abs_pwned.txt"
    files = [GeneratedFile(filename=str(target), content="PWNED", language="python")]

    fg._validate_generated_files(files)

    assert not target.exists(), "absolute filename was written outside the temp dir"


def test_normal_filename_still_written(controlled_tmp):
    files = [GeneratedFile(filename="command.py", content="ok", language="python")]
    result = fg._validate_generated_files(files)
    # a safe filename produces no "unsafe filename" error
    assert not any("unsafe filename" in e for e in result.errors)
