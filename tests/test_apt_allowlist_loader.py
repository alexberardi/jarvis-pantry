"""Tests for the apt allow-list loader (#16)."""

from pathlib import Path

import pytest
import yaml

from app.services.apt_allowlist import (
    AptAllowlist,
    AptAllowlistEntry,
    AptAllowlistLoadError,
    load_allowlist,
)


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "apt-allowlist.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _entry(name: str, reason: str = "needed for tests") -> dict:
    return {
        "name": name,
        "reason": reason,
        "added_by": "alex",
        "added_at": "2026-05-18",
    }


class TestLoaderHappyPath:
    def test_loads_valid_yaml_returns_entries(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv"), _entry("vlc"), _entry("ffmpeg")]})
        allowlist = load_allowlist(path)
        assert isinstance(allowlist, AptAllowlist)
        assert allowlist.is_allowed("mpv") is True
        assert allowlist.is_allowed("vlc") is True
        assert allowlist.is_allowed("ffmpeg") is True

    def test_is_allowed_returns_true_for_on_list_package(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv")]})
        assert load_allowlist(path).is_allowed("mpv") is True

    def test_is_allowed_returns_false_for_off_list_package(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv")]})
        assert load_allowlist(path).is_allowed("postgresql-server") is False

    def test_find_returns_entry_metadata_for_on_list_package(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [
            {
                "name": "mpv",
                "reason": "media playback",
                "added_by": "alex",
                "added_at": "2026-05-18",
            }
        ]})
        entry = load_allowlist(path).find("mpv")
        assert entry is not None
        assert isinstance(entry, AptAllowlistEntry)
        assert entry.name == "mpv"
        assert entry.reason == "media playback"
        assert entry.added_by == "alex"
        assert entry.added_at == "2026-05-18"

    def test_find_returns_none_for_off_list_package(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv")]})
        assert load_allowlist(path).find("postgresql-server") is None


class TestLoaderEdgeCases:
    def test_case_sensitive_lookup(self, tmp_path):
        """apt is case-sensitive; the allow-list should match its behavior."""
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv")]})
        allowlist = load_allowlist(path)
        assert allowlist.is_allowed("MPV") is False
        assert allowlist.is_allowed("Mpv") is False

    def test_empty_packages_list(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": []})
        allowlist = load_allowlist(path)
        assert allowlist.is_allowed("anything") is False
        assert allowlist.find("anything") is None

    def test_duplicate_entries_dedupe_silently(self, tmp_path):
        """Defensive against hand-edit mistakes — duplicate names shouldn't blow up."""
        path = _write_yaml(tmp_path, {"packages": [_entry("mpv"), _entry("mpv", "again")]})
        allowlist = load_allowlist(path)
        assert allowlist.is_allowed("mpv") is True
        # The first entry wins for the canonical metadata.
        entry = allowlist.find("mpv")
        assert entry is not None
        assert entry.reason == "needed for tests"


class TestLoaderFailsClosed:
    def test_malformed_yaml_raises(self, tmp_path):
        """Loader fails closed on malformed YAML — never silently accepts everything."""
        p = tmp_path / "apt-allowlist.yaml"
        p.write_text('packages: [{name: "mpv"\n')  # truncated/invalid
        with pytest.raises(AptAllowlistLoadError):
            load_allowlist(p)

    def test_missing_file_raises(self, tmp_path):
        """Missing config file must fail loud — silent fallback to empty would defeat the gate."""
        with pytest.raises(FileNotFoundError):
            load_allowlist(tmp_path / "does-not-exist.yaml")

    def test_entry_missing_name_raises(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": [{"reason": "no name", "added_by": "a", "added_at": "2026-05-18"}]})
        with pytest.raises(AptAllowlistLoadError):
            load_allowlist(path)

    def test_packages_not_a_list_raises(self, tmp_path):
        path = _write_yaml(tmp_path, {"packages": "mpv"})
        with pytest.raises(AptAllowlistLoadError):
            load_allowlist(path)

    def test_top_level_not_a_dict_raises(self, tmp_path):
        p = tmp_path / "apt-allowlist.yaml"
        p.write_text("- mpv\n- vlc\n")
        with pytest.raises(AptAllowlistLoadError):
            load_allowlist(p)
