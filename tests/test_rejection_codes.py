"""Tests for the rejection_codes module (Finding dataclass + reason code catalogue)."""

from __future__ import annotations

import re

from app.services.rejection_codes import (
    ALL_REASON_CODES,
    Finding,
    doc_url,
)


REQUIRED_CODES = {
    "static_analysis_disallowed_primitive",
    "apt_package_not_on_allowlist",
    "manifest_bad_semver",
    "manifest_missing_required_field",
    "manifest_invalid_field_type",
    "legacy_unstructured",
}


class TestReasonCodeCatalog:
    def test_required_codes_are_defined(self):
        # Superset so future codes (#14/#16) can be added without breaking this test.
        assert REQUIRED_CODES.issubset(ALL_REASON_CODES)

    def test_codes_are_plain_strings(self):
        pat = re.compile(r"^[a-z][a-z0-9_]*$")
        for code in ALL_REASON_CODES:
            assert pat.match(code), f"reason_code {code!r} is not wire-safe snake_case"


class TestFindingDataclass:
    def test_constructs_with_required_fields_only(self):
        f = Finding(
            reason_code="manifest_bad_semver",
            severity="error",
            doc_url="https://docs.jarvisautomation.dev/pantry/rejections#manifest_bad_semver",
        )
        assert f.file is None
        assert f.line is None
        assert f.snippet is None
        assert f.primitive is None
        assert f.value is None
        assert f.message is None

    def test_constructs_with_all_fields(self):
        f = Finding(
            reason_code="static_analysis_disallowed_primitive",
            severity="error",
            doc_url="https://docs.jarvisautomation.dev/pantry/rejections#static_analysis_disallowed_primitive",
            primitive="eval",
            file="my_command/command.py",
            line=42,
            snippet="result = eval(user_input)",
            value="some_value",
            message="some message",
        )
        assert f.primitive == "eval"
        assert f.file == "my_command/command.py"
        assert f.line == 42
        assert f.snippet == "result = eval(user_input)"
        assert f.value == "some_value"
        assert f.message == "some message"

    def test_to_dict_omits_none_fields(self):
        f = Finding(
            reason_code="manifest_bad_semver",
            severity="error",
            value="abc",
            doc_url="https://docs.jarvisautomation.dev/pantry/rejections#manifest_bad_semver",
        )
        d = f.to_dict()
        assert d == {
            "reason_code": "manifest_bad_semver",
            "severity": "error",
            "value": "abc",
            "doc_url": "https://docs.jarvisautomation.dev/pantry/rejections#manifest_bad_semver",
        }
        assert "file" not in d
        assert "line" not in d
        assert "snippet" not in d
        assert "primitive" not in d
        assert "message" not in d

    def test_to_dict_full_shape_matches_breakdown_example(self):
        f = Finding(
            reason_code="static_analysis_disallowed_primitive",
            severity="error",
            primitive="eval",
            file="my_command/command.py",
            line=42,
            snippet="result = eval(user_input)",
            doc_url="https://docs.jarvisautomation.dev/pantry/rejections#static_analysis_disallowed_primitive",
        )
        assert f.to_dict() == {
            "reason_code": "static_analysis_disallowed_primitive",
            "severity": "error",
            "primitive": "eval",
            "file": "my_command/command.py",
            "line": 42,
            "snippet": "result = eval(user_input)",
            "doc_url": "https://docs.jarvisautomation.dev/pantry/rejections#static_analysis_disallowed_primitive",
        }


class TestDocUrlMapper:
    def test_each_known_code_returns_docs_jarvisautomation_dev_url(self):
        for code in ALL_REASON_CODES:
            url = doc_url(code)
            assert url.startswith("https://docs.jarvisautomation.dev/"), url
            assert url.endswith(f"#{code}"), url

    def test_unknown_code_still_returns_doc_url(self):
        # Defensive — an unknown code shouldn't break the envelope.
        url = doc_url("totally_made_up_code")
        assert url.startswith("https://docs.jarvisautomation.dev/")
        assert "totally_made_up_code" in url
