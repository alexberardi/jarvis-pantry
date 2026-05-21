"""Tests for the shipped apt-source allow-list YAML.

Mirrors test_apt_allowlist_config.py — intentionally rigid so accidental
additions/removals to the seed list show up as a failing test and force
the change through PR review.
"""

from datetime import date
from pathlib import Path

import yaml

import app


REPO_ROOT = Path(app.__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "apt-source-allowlist.yaml"

EXPECTED_SEED = {
    "raspotify",
}


def _load() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


class TestShippedAptSourceAllowlistConfig:
    def test_shipped_yaml_exists_and_parses(self):
        assert CONFIG_PATH.exists(), f"missing: {CONFIG_PATH}"
        data = _load()
        assert isinstance(data, dict)
        assert isinstance(data.get("sources"), list)

    def test_shipped_yaml_matches_expected_seed_set(self):
        data = _load()
        names = {entry["name"] for entry in data["sources"]}
        assert names == EXPECTED_SEED
        assert len(data["sources"]) == len(EXPECTED_SEED)

    def test_shipped_yaml_entries_have_required_fields(self):
        data = _load()
        for entry in data["sources"]:
            for key in ("name", "key_url", "repo", "reason", "added_by", "added_at"):
                assert key in entry, f"entry {entry!r} missing {key}"
            assert isinstance(entry["name"], str) and entry["name"]
            assert isinstance(entry["key_url"], str) and entry["key_url"].startswith("https://")
            assert isinstance(entry["repo"], str)
            assert entry["repo"].startswith("deb ") or entry["repo"].startswith("deb-src ")
            assert isinstance(entry["reason"], str) and entry["reason"]
            added_at = entry["added_at"]
            if isinstance(added_at, str):
                date.fromisoformat(added_at)
            else:
                assert isinstance(added_at, date)

    def test_no_duplicate_names_in_shipped_yaml(self):
        data = _load()
        names = [entry["name"] for entry in data["sources"]]
        assert len(names) == len(set(names))
