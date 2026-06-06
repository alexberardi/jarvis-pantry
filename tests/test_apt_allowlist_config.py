"""Tests for the shipped apt allow-list YAML (#16, v1 seed).

These are intentionally rigid — accidental additions/removals to the seed list
should show up as a failing test, forcing the change through PR review.
"""

from datetime import date
from pathlib import Path

import yaml

import app


REPO_ROOT = Path(app.__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "apt-allowlist.yaml"

EXPECTED_SEED = {
    "mpv", "vlc", "ffmpeg", "alsa-utils", "sox", "mopidy",
    "pulseaudio", "pipewire-pulse", "bluez", "yt-dlp", "imagemagick",
    "shairport-sync", "raspotify", "mpd",
}


def _load() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


class TestShippedAllowlistConfig:
    def test_shipped_yaml_exists_and_parses(self):
        assert CONFIG_PATH.exists(), f"missing: {CONFIG_PATH}"
        data = _load()
        assert isinstance(data, dict)
        assert isinstance(data.get("packages"), list)

    def test_shipped_yaml_matches_expected_seed_set(self):
        data = _load()
        names = {entry["name"] for entry in data["packages"]}
        assert names == EXPECTED_SEED
        assert len(data["packages"]) == len(EXPECTED_SEED)

    def test_shipped_yaml_entries_have_required_fields(self):
        data = _load()
        for entry in data["packages"]:
            for key in ("name", "reason", "added_by", "added_at"):
                assert key in entry, f"entry {entry!r} missing {key}"
            assert isinstance(entry["name"], str) and entry["name"]
            assert isinstance(entry["reason"], str) and entry["reason"]
            # added_at should be ISO date or yaml-native date.
            added_at = entry["added_at"]
            if isinstance(added_at, str):
                # ISO 8601 — yyyy-mm-dd parses cleanly.
                date.fromisoformat(added_at)
            else:
                assert isinstance(added_at, date)

    def test_no_duplicate_names_in_shipped_yaml(self):
        data = _load()
        names = [entry["name"] for entry in data["packages"]]
        assert len(names) == len(set(names))
